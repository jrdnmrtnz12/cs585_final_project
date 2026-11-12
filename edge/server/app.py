"""Edge server: FastAPI app exposing /health and /infer (see edge/protocol.py).

The detector is created once, before the app starts, and shared by every
request. Inference runs in a worker thread so /health stays responsive, and
the detector serializes model access itself.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import asynccontextmanager

import cv2
import numpy as np
import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from edge import protocol as P
from edge.server.detector import Detector
from edge.server.udp_echo import UdpEcho

log = logging.getLogger(__name__)


class ResourceSampler:
    """Samples this process's CPU% and RSS once per second into a bounded buffer."""

    def __init__(self, period_s: float = 1.0, max_samples: int = 3600):
        self.period_s = period_s
        self.samples: deque[tuple[float, float, float]] = deque(maxlen=max_samples)
        self._proc = psutil.Process()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="server-resources", daemon=True)

    def start(self) -> None:
        self._proc.cpu_percent(None)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        self.samples.clear()

    def _run(self) -> None:
        while not self._stop.wait(self.period_s):
            cpu = self._proc.cpu_percent(None)
            rss_mb = self._proc.memory_info().rss / 1e6
            self.samples.append((time.monotonic(), cpu, rss_mb))

    def summary(self) -> dict[str, float | int | None]:
        s = list(self.samples)
        if not s:
            return {"n_samples": 0, "cpu_pct_mean": None, "cpu_pct_max": None, "rss_mb_max": None}
        cpus = [c for _, c, _ in s]
        return {"n_samples": len(s), "cpu_pct_mean": sum(cpus) / len(cpus),
                "cpu_pct_max": max(cpus), "rss_mb_max": max(r for _, _, r in s)}


def create_app(detector: Detector, udp_port: int | None = 9000, udp_host: str = "0.0.0.0") -> FastAPI:
    """Build the app. udp_port=None disables the UDP echo (used in unit tests)."""
    echo = UdpEcho(udp_host, udp_port) if udp_port is not None else None
    resources = ResourceSampler()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if echo:
            echo.start()
        resources.start()
        yield
        resources.stop()
        if echo:
            echo.stop()

    app = FastAPI(title="CS585 edge server", lifespan=lifespan)
    app.state.detector = detector
    app.state.echo = echo

    @app.get(P.HEALTH_PATH, response_model=P.HealthResponse)
    def health() -> P.HealthResponse:
        return P.HealthResponse(status="ok", detector=detector.name, mock=detector.is_mock,
                                device=detector.device, imgsz=detector.imgsz, conf=detector.conf,
                                weights=detector.weights, library_version=detector.library_version)

    @app.post(P.STATS_RESET_PATH)
    def stats_reset() -> dict[str, str]:
        resources.reset()
        return {"status": "reset"}

    @app.get(P.STATS_PATH)
    def stats() -> dict[str, float | int | None]:
        return resources.summary()

    @app.post(P.INFER_PATH, response_model=P.InferResponse)
    async def infer(request: Request) -> P.InferResponse:
        raw_id = request.headers.get(P.HDR_FRAME_ID)
        try:
            frame_id = int(raw_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"missing or invalid {P.HDR_FRAME_ID} header")
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="empty body; expected JPEG bytes")
        # Server clock starts once the whole body has arrived, so upload time is NOT included.
        return await run_in_threadpool(_process, detector, frame_id, body)

    return app


def _process(detector: Detector, frame_id: int, body: bytes) -> P.InferResponse:
    t0 = time.perf_counter()
    img = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
    t1 = time.perf_counter()
    if img is None:
        raise HTTPException(status_code=400, detail="body is not a decodable JPEG")
    dets = detector.detect(img)
    t2 = time.perf_counter()
    h, w = img.shape[:2]
    resp = P.InferResponse(
        frame_id=frame_id,
        image_wh=[w, h],
        detections=[P.Detection(class_id=d.class_id, label=d.label, conf=round(d.conf, 4),
                                bbox_xyxy_px=[round(v, 1) for v in d.bbox_xyxy_px]) for d in dets],
        timing=P.Timing(decode_ms=(t1 - t0) * 1e3, infer_ms=(t2 - t1) * 1e3,
                        server_total_ms=(time.perf_counter() - t0) * 1e3),
        mock=detector.is_mock,
    )
    return resp
