"""Object detectors used by the edge server.

MockDetector exists so networking and API work can proceed without a model.
It returns NO detections and every response is tagged mock=true, so mock
output can never be mistaken for detection accuracy.

YoloDetector wraps a pretrained Ultralytics YOLO model. The model is loaded
once at startup and warmed up. Every input is letterboxed to `imgsz` pixels,
so inference cost is roughly the same for every encoding profile; the
profiles differ in network cost and in the information lost to downscaling
and JPEG compression.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Det:
    class_id: int
    label: str
    conf: float
    bbox_xyxy_px: tuple[float, float, float, float]


class Detector(Protocol):
    name: str
    is_mock: bool
    device: str
    imgsz: int
    conf: float
    weights: str | None
    library_version: str | None

    def detect(self, bgr: np.ndarray) -> list[Det]: ...


class MockDetector:
    """Sleeps for a fixed time and returns nothing. For testing the pipeline only."""

    name = "mock"
    is_mock = True
    device = "none"
    weights = None
    library_version = None

    def __init__(self, delay_ms: float = 15.0, imgsz: int = 640, conf: float = 0.25):
        self.delay_s = delay_ms / 1000.0
        self.imgsz = imgsz
        self.conf = conf

    def detect(self, bgr: np.ndarray) -> list[Det]:
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        return []


MODELS_DIR = Path(__file__).resolve().parents[2] / "models"


def _resolve_weights(weights: str) -> str:
    """A bare filename like 'yolo11n.pt' resolves to models/yolo11n.pt (gitignored)."""
    p = Path(weights)
    if p.exists() or p.parent != Path("."):
        return str(p)
    MODELS_DIR.mkdir(exist_ok=True)
    return str(MODELS_DIR / p.name)


def resolve_device(requested: str) -> str:
    """'auto' picks cuda, then Apple 'mps', then 'cpu'."""
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda:0"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class YoloDetector:
    """Pretrained YOLO detector (COCO classes). Thread-safe: one inference at a time."""

    is_mock = False

    def __init__(self, weights: str = "yolo11n.pt", device: str = "auto", imgsz: int = 640,
                 conf: float = 0.25, warmup_runs: int = 3):
        import ultralytics
        from ultralytics import YOLO

        self.weights = _resolve_weights(weights)
        self.device = resolve_device(device)
        self.imgsz = imgsz
        self.conf = conf
        self.library_version = f"ultralytics {ultralytics.__version__}"
        self.name = f"yolo:{Path(self.weights).name}"
        self._model = YOLO(self.weights)  # downloads the official weights on first use if missing
        self._names: dict[int, str] = dict(self._model.names)
        self._lock = threading.Lock()
        dummy = np.zeros((imgsz * 9 // 16, imgsz, 3), dtype=np.uint8)
        for _ in range(warmup_runs):
            self.detect(dummy)

    def detect(self, bgr: np.ndarray) -> list[Det]:
        with self._lock:
            result = self._model.predict(bgr, imgsz=self.imgsz, conf=self.conf,
                                         device=self.device, verbose=False)[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        xyxy = boxes.xyxy.cpu().numpy()
        cls = boxes.cls.cpu().numpy().astype(int)
        confs = boxes.conf.cpu().numpy()
        return [
            Det(int(c), self._names.get(int(c), str(c)), float(p),
                (float(b[0]), float(b[1]), float(b[2]), float(b[3])))
            for b, c, p in zip(xyxy, cls, confs)
        ]
