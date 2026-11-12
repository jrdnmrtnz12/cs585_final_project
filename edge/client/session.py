"""One client session: capture -> encode -> send -> log, with the control loop.

Threads:
  capture   CaptureLoop: grabs frames at the current profile's fps into the slot
  sender    takes the newest frame, encodes it, POSTs it, logs the result
  probe-tx/probe-rx  UDP RTT/loss probes
  caller    the control loop: once per window it computes WindowStats, asks
            the policy for a decision, and writes netmon/switch/resource rows

All timestamps use one clock: time.monotonic_ns() minus the trial start.
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import psutil

from edge.adaptation.controller import AdaptiveController, FixedPolicy, WindowStats
from edge.client.capture import FrameSource
from edge.client.encoder import encode_frame
from edge.client.netmon import GoodputEstimator, UdpProber
from edge.client.sender import CaptureLoop, HttpSender, LatestFrameSlot
from edge.config import ExperimentConfig, Profile
from edge.logging_utils import (EVENTS_COLUMNS, FRAMES_COLUMNS, NETMON_COLUMNS, RESOURCES_COLUMNS,
                                SWITCHES_COLUMNS, CsvLog)

log = logging.getLogger(__name__)

Policy = AdaptiveController | FixedPolicy


class ClientSession:
    def __init__(self, *, exp: ExperimentConfig, profiles: dict[str, Profile], policy: Policy,
                 trial_id: str, trial_dir: Path, source: FrameSource, server_host: str):
        self.exp, self.profiles, self.policy = exp, profiles, policy
        self.trial_id = trial_id
        self._t0 = time.monotonic_ns()
        self.clock_ns: Callable[[], int] = lambda: time.monotonic_ns() - self._t0

        d = Path(trial_dir)
        self.frames = CsvLog(d / "frames.csv", FRAMES_COLUMNS, trial_id)
        self.netmon = CsvLog(d / "netmon.csv", NETMON_COLUMNS, trial_id)
        self.switches = CsvLog(d / "switches.csv", SWITCHES_COLUMNS, trial_id)
        self.events = CsvLog(d / "events.csv", EVENTS_COLUMNS, trial_id)
        self.resources = CsvLog(d / "resources.csv", RESOURCES_COLUMNS, trial_id)

        self.stop_event = threading.Event()
        self.slot = LatestFrameSlot()
        self.capture = CaptureLoop(source, self.slot, lambda: self._profile().fps, self.clock_ns,
                                   self._on_superseded, self._on_capture_error, self.stop_event)
        self.sender = HttpSender(f"http://{server_host}:{exp.http_port}", exp.request_timeout_s,
                                 trial_id, self.clock_ns)
        self.prober = UdpProber(server_host, exp.udp_port, exp.netmon.probe_hz,
                                exp.netmon.probe_timeout_s, self.clock_ns)
        self.goodput = GoodputEstimator(exp.netmon.goodput_ewma_alpha, exp.netmon.min_transfer_ms)
        self._sender_thread = threading.Thread(target=self._sender_loop, name="sender", daemon=True)

        self._recent: deque[tuple[float, float]] = deque()  # (latency_ms, server_ms) since last window
        self._recent_lock = threading.Lock()
        self._inflight_capture_ns: int | None = None
        self._base_rtt_ms: float | None = None
        self._proc = psutil.Process()
        self.counts = {"sent": 0, "ok": 0, "timeout": 0, "http_error": 0, "conn_error": 0, "superseded": 0}
        self.capture_error: str | None = None

    # ---- callbacks / helpers -----------------------------------------
    def _profile(self) -> Profile:
        return self.profiles[self.policy.profile]

    def log_event(self, event: str, detail: str = "", frame_id: int | None = None) -> None:
        self.events.write(t_ns=self.clock_ns(), event=event, frame_id=frame_id, detail=detail)

    def _on_superseded(self, frame_id: int, t_ns: int) -> None:
        self.counts["superseded"] += 1
        self.events.write(t_ns=t_ns, event="superseded", frame_id=frame_id)

    def _on_capture_error(self, e: Exception) -> None:
        self.capture_error = f"{type(e).__name__}: {e}"
        self.log_event("capture_error", self.capture_error)
        self.stop_event.set()

    # ---- sender thread -----------------------------------------------
    def _sender_loop(self) -> None:
        while not self.stop_event.is_set():
            item = self.slot.take(timeout=0.2)
            if item is None:
                continue
            prof = self._profile()
            enc = encode_frame(item.image, prof.width, prof.height, prof.jpeg_quality)
            self._inflight_capture_ns = item.capture_ns
            res = self.sender.send(item.frame_id, prof.name, enc)
            self._inflight_capture_ns = None
            self.counts["sent"] += 1
            self.counts[res.outcome] += 1
            timing = res.response.timing if res.response else None
            latency_ms = (res.recv_ns - item.capture_ns) / 1e6
            rtt_ms = (res.recv_ns - res.send_ns) / 1e6
            self.frames.write(
                frame_id=item.frame_id, profile=prof.name,
                capture_ns=item.capture_ns, send_ns=res.send_ns, recv_ns=res.recv_ns,
                encode_ms=enc.encode_ms, jpeg_bytes=len(enc.data), width_px=enc.width, height_px=enc.height,
                jpeg_quality=enc.quality, http_status=res.http_status,
                server_decode_ms=timing.decode_ms if timing else None,
                server_infer_ms=timing.infer_ms if timing else None,
                server_total_ms=timing.server_total_ms if timing else None,
                n_detections=len(res.response.detections) if res.response else None,
                latency_ms=latency_ms, request_rtt_ms=rtt_ms, outcome=res.outcome,
            )
            if res.outcome == "ok" and timing is not None:
                self.policy.observe_frame_size(prof.name, len(enc.data))
                self.goodput.add(len(enc.data), rtt_ms, timing.server_total_ms, self._base_rtt_ms)
                with self._recent_lock:
                    self._recent.append((latency_ms, timing.server_total_ms))
            else:
                self.events.write(t_ns=res.recv_ns, event=res.outcome, frame_id=item.frame_id, detail=res.detail)
                if res.outcome == "conn_error":
                    self.stop_event.wait(0.2)  # avoid a hot loop while the server is unreachable

    # ---- control loop ------------------------------------------------
    def _control_window(self, window_s: float) -> None:
        t_end = self.clock_ns()
        pm = self.prober.window(t_end, int(window_s * 1e9))
        self._base_rtt_ms = pm["rtt_ms_base"]  # type: ignore[assignment]
        gp = self.goodput.close_window()
        with self._recent_lock:
            recent, self._recent = list(self._recent), deque()
        lat = [r[0] for r in recent]
        srv = [r[1] for r in recent]
        inflight = self._inflight_capture_ns
        stats = WindowStats(
            t_s=t_end / 1e9, rtt_ms=pm["rtt_ms_base"], loss_pct=pm["loss_pct"], goodput_mbps=gp,
            latency_ms_median=statistics.median(lat) if lat else None, n_responses=len(lat),
            server_ms=statistics.median(srv) if srv else None,
            oldest_inflight_ms=(t_end - inflight) / 1e6 if inflight is not None else None,
        )
        before = self.policy.profile
        decision = self.policy.step(stats)
        self.netmon.write(t_ns=t_end, profile=before, rtt_ms_min=pm["rtt_ms_min"], rtt_ms_base=pm["rtt_ms_base"],
                          rtt_ms_median=pm["rtt_ms_median"], loss_pct=pm["loss_pct"],
                          probes_sent=pm["probes_sent"], probes_lost=pm["probes_lost"], goodput_mbps_est=gp,
                          latency_ms_median=stats.latency_ms_median, n_responses=stats.n_responses,
                          server_ms_median=stats.server_ms, oldest_inflight_ms=stats.oldest_inflight_ms)
        if decision.changed:
            self.switches.write(t_ns=t_end, from_profile=before, to_profile=decision.profile, reason=decision.reason,
                                rtt_ms=stats.rtt_ms, loss_pct=stats.loss_pct, goodput_mbps=gp,
                                latency_ms_median=stats.latency_ms_median, predicted_ms=decision.predicted_ms)
            log.info("t=%.1fs switch %s -> %s (%s)", stats.t_s, before, decision.profile, decision.reason)
        self.resources.write(t_ns=t_end, proc_cpu_pct=self._proc.cpu_percent(None),
                             sys_cpu_pct=psutil.cpu_percent(None), proc_rss_mb=self._proc.memory_info().rss / 1e6)

    def run(self, duration_s: float) -> dict[str, object]:
        """Run for duration_s seconds (trial clock). Always stops threads and closes logs."""
        window_s = self.exp.controller.window_s
        self._proc.cpu_percent(None)
        psutil.cpu_percent(None)
        self.prober.start()
        self.capture.start()
        self._sender_thread.start()
        self.log_event("start", f"profile={self.policy.profile}")
        status = "complete"
        try:
            next_w = window_s
            while not self.stop_event.is_set():
                now_s = self.clock_ns() / 1e9
                if now_s >= duration_s:
                    break
                wait = min(next_w, duration_s) - now_s
                if wait > 0 and self.stop_event.wait(wait):
                    break
                if self.clock_ns() / 1e9 >= next_w - 1e-3:
                    self._control_window(window_s)
                    next_w += window_s
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        finally:
            if self.capture_error:
                status = "capture_error"
            self.log_event("stop", status)
            self.stop_event.set()
            self.slot.close()
            self.capture.join(timeout=3)
            self._sender_thread.join(timeout=self.exp.request_timeout_s + 2)
            self.prober.stop()
            self.sender.close()
            for lg in (self.frames, self.netmon, self.switches, self.events, self.resources):
                lg.close()
        return {"status": status, "duration_s": duration_s, "captured": self.capture.captured,
                "capture_error": self.capture_error, **self.counts}
