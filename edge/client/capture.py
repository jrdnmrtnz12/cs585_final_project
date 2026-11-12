"""Frame sources for the client.

ScreenSource is the EXPERIMENT mode from the proposal: live screen capture of
a repeatable video playing on the client's display.

VideoFileSource and SyntheticSource are for development and automated tests
only. Results produced with them must not be reported as experiment data
(metadata.json records which source each trial used).

Linux note: mss needs an X11 session. Under Wayland, log in with
"Ubuntu on Xorg" (or equivalent) before running trials.
macOS note: the terminal app needs Screen Recording permission.
"""
from __future__ import annotations

import time
from typing import Protocol

import cv2
import numpy as np


class FrameSource(Protocol):
    kind: str

    def read(self) -> np.ndarray:
        """Return the current frame as a BGR uint8 array."""
        ...

    def close(self) -> None: ...


class ScreenSource:
    kind = "screen"

    def __init__(self, monitor: int = 1, region: dict[str, int] | None = None):
        self.monitor = monitor
        self.region = region
        self._sct = None  # created lazily in the capture thread (mss handles are per-thread)
        self._area: dict[str, int] | None = None

    def read(self) -> np.ndarray:
        if self._sct is None:
            import mss

            self._sct = mss.mss()
            self._area = dict(self.region) if self.region else dict(self._sct.monitors[self.monitor])
        shot = self._sct.grab(self._area)
        return cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


class VideoFileSource:
    """Reads a video file as if it were playing in real time, looping at the end.

    read() returns the frame that would be on screen *now*, skipping frames as
    needed, so a slow consumer sees the video advance at its natural speed.
    """

    kind = "video"

    def __init__(self, path: str):
        self.path = path
        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            raise FileNotFoundError(f"cannot open video {path}")
        self.fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._t0 = time.monotonic()
        self._pos = -1
        self._frame: np.ndarray | None = None

    def read(self, _retry: bool = True) -> np.ndarray:
        target = int((time.monotonic() - self._t0) * self.fps)
        while self._pos < target - 1:  # behind schedule: skip frames without decoding them
            if not self._cap.grab():
                return self._loop(_retry)
            self._pos += 1
        if self._pos < target or self._frame is None:
            ok, frame = self._cap.read()
            if not ok:
                return self._loop(_retry)
            self._frame = frame
            self._pos += 1
        return self._frame

    def _loop(self, retry: bool) -> np.ndarray:
        """End of file: rewind and restart the playback clock."""
        if not retry:
            raise RuntimeError(f"video {self.path} has no readable frames")
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        self._t0 = time.monotonic()
        self._pos = -1
        self._frame = None
        return self.read(_retry=False)

    def close(self) -> None:
        self._cap.release()


class SyntheticSource:
    """Moving shapes on a gradient. No video file needed; for tests and smoke runs."""

    kind = "synthetic"

    def __init__(self, width: int = 1280, height: int = 720):
        self.width, self.height = width, height
        x = np.linspace(0, 255, width, dtype=np.uint8)
        self._bg = np.dstack([np.tile(x, (height, 1))] * 3)
        self._t0 = time.monotonic()

    def read(self) -> np.ndarray:
        t = time.monotonic() - self._t0
        img = self._bg.copy()
        cx = int((np.sin(t) * 0.4 + 0.5) * self.width)
        cy = int((np.cos(t * 0.7) * 0.3 + 0.5) * self.height)
        cv2.circle(img, (cx, cy), self.height // 6, (0, 0, 255), -1)
        cv2.rectangle(img, (self.width - cx - 80, cy - 60), (self.width - cx + 80, cy + 60), (255, 0, 0), -1)
        cv2.putText(img, f"{t:7.2f}", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3)
        return img

    def close(self) -> None:
        pass


def make_source(kind: str, video_path: str = "", monitor: int = 1,
                region: dict[str, int] | None = None) -> FrameSource:
    if kind == "screen":
        return ScreenSource(monitor=monitor, region=region)
    if kind == "video":
        return VideoFileSource(video_path)
    if kind == "synthetic":
        return SyntheticSource()
    raise ValueError(f"unknown source kind {kind!r}")
