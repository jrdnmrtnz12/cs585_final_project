"""Capture thread, latest-frame slot, and HTTP sender.

Queueing policy (bounded by design):
  * The capture thread grabs frames at the CURRENT profile's fps and puts
    each one into a single-slot buffer.
  * If the slot still holds an unsent frame, that frame is SUPERSEDED (replaced
    by the newer one) and logged. It is never silently discarded.
  * The sender takes the newest frame, encodes it with the current profile,
    and sends it. At most ONE request is in flight. For an interactive
    wearable, the newest frame is the useful one, so no backlog of stale
    frames can build up on the client.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx
import numpy as np

from edge import protocol as P
from edge.client.capture import FrameSource
from edge.client.encoder import EncodedFrame


@dataclass
class SlotItem:
    frame_id: int
    capture_ns: int
    image: np.ndarray


class LatestFrameSlot:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: SlotItem | None = None
        self._closed = False

    def put(self, item: SlotItem) -> int | None:
        """Store item. Returns the frame_id it superseded, or None."""
        with self._cond:
            superseded = self._item.frame_id if self._item is not None else None
            self._item = item
            self._cond.notify()
            return superseded

    def take(self, timeout: float) -> SlotItem | None:
        with self._cond:
            if self._item is None and not self._closed:
                self._cond.wait(timeout)
            item, self._item = self._item, None
            return item

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class CaptureLoop(threading.Thread):
    def __init__(self, source: FrameSource, slot: LatestFrameSlot, fps_fn: Callable[[], float],
                 clock_ns: Callable[[], int], on_superseded: Callable[[int, int], None],
                 on_error: Callable[[Exception], None], stop: threading.Event):
        super().__init__(name="capture", daemon=True)
        self.source, self.slot, self.fps_fn, self.clock_ns = source, slot, fps_fn, clock_ns
        self.on_superseded, self.on_error, self.stop_event = on_superseded, on_error, stop
        self.ids = itertools.count(1)
        self.captured = 0

    def run(self) -> None:
        next_t = time.monotonic()
        try:
            while not self.stop_event.is_set():
                capture_ns = self.clock_ns()  # timestamp at the START of the grab
                img = self.source.read()
                fid = next(self.ids)
                self.captured += 1
                old = self.slot.put(SlotItem(fid, capture_ns, img))
                if old is not None:
                    self.on_superseded(old, capture_ns)
                period = 1.0 / self.fps_fn()
                next_t += period
                delay = next_t - time.monotonic()
                if delay < -period:     # fell behind (slow grab): resync instead of bursting
                    next_t = time.monotonic()
                elif delay > 0:
                    self.stop_event.wait(delay)
        except Exception as e:  # surface capture failures instead of dying silently
            self.on_error(e)
        finally:
            self.source.close()
            self.slot.close()


@dataclass
class SendResult:
    send_ns: int
    recv_ns: int
    http_status: int | None
    response: P.InferResponse | None
    outcome: str  # ok | timeout | http_error | conn_error
    detail: str = ""


class HttpSender:
    """One persistent HTTP/1.1 connection (keep-alive) to the edge server."""

    def __init__(self, base_url: str, timeout_s: float, trial_id: str, clock_ns: Callable[[], int]):
        self.trial_id = trial_id
        self.clock_ns = clock_ns
        self.client = httpx.Client(base_url=base_url, timeout=timeout_s,
                                   headers={"Content-Type": P.JPEG_CONTENT_TYPE, P.HDR_TRIAL_ID: trial_id})

    def send(self, frame_id: int, profile: str, enc: EncodedFrame) -> SendResult:
        headers = {P.HDR_FRAME_ID: str(frame_id), P.HDR_PROFILE: profile}
        send_ns = self.clock_ns()
        try:
            r = self.client.post(P.INFER_PATH, content=enc.data, headers=headers)
            recv_ns = self.clock_ns()
        except httpx.TimeoutException as e:
            return SendResult(send_ns, self.clock_ns(), None, None, "timeout", type(e).__name__)
        except httpx.HTTPError as e:
            return SendResult(send_ns, self.clock_ns(), None, None, "conn_error", f"{type(e).__name__}: {e}")
        if r.status_code != 200:
            return SendResult(send_ns, recv_ns, r.status_code, None, "http_error", r.text[:200])
        resp = P.InferResponse.model_validate(r.json())
        if resp.frame_id != frame_id:
            return SendResult(send_ns, recv_ns, r.status_code, resp, "http_error",
                              f"frame_id mismatch: sent {frame_id}, got {resp.frame_id}")
        return SendResult(send_ns, recv_ns, r.status_code, resp, "ok")

    def close(self) -> None:
        self.client.close()
