"""Resize + JPEG-encode a frame according to an encoding profile.

Transformation (documented for the methods section):
  1. If the source aspect ratio differs from the target's by more than 1%,
     center-crop the source to the target aspect ratio (never stretch).
  2. Resize to the profile's width x height (INTER_AREA when shrinking,
     INTER_LINEAR when enlarging).
  3. JPEG-encode at the profile's quality (OpenCV/libjpeg, 4:2:0 default).
encode_ms covers all three steps.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class EncodedFrame:
    data: bytes
    width: int
    height: int
    quality: int
    encode_ms: float


def crop_to_aspect(img: np.ndarray, width: int, height: int) -> np.ndarray:
    h, w = img.shape[:2]
    target = width / height
    src = w / h
    if abs(src - target) / target <= 0.01:
        return img
    if src > target:  # too wide: trim left and right
        new_w = int(round(h * target))
        x0 = (w - new_w) // 2
        return img[:, x0:x0 + new_w]
    new_h = int(round(w / target))  # too tall: trim top and bottom
    y0 = (h - new_h) // 2
    return img[y0:y0 + new_h, :]


def encode_frame(bgr: np.ndarray, width: int, height: int, quality: int) -> EncodedFrame:
    t0 = time.perf_counter()
    img = crop_to_aspect(bgr, width, height)
    h, w = img.shape[:2]
    if (w, h) != (width, height):
        interp = cv2.INTER_AREA if w > width else cv2.INTER_LINEAR
        img = cv2.resize(img, (width, height), interpolation=interp)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return EncodedFrame(buf.tobytes(), width, height, int(quality), (time.perf_counter() - t0) * 1e3)
