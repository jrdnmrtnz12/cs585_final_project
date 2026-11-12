from __future__ import annotations

import cv2
import numpy as np

from edge.client.capture import SyntheticSource
from edge.client.encoder import crop_to_aspect, encode_frame
from edge.config import load_profiles


def _frame(w: int = 1280, h: int = 720) -> np.ndarray:
    return cv2.resize(SyntheticSource(1280, 720).read(), (w, h))


def test_output_size_and_decodable():
    profiles, order = load_profiles()
    for name in order:
        p = profiles[name]
        enc = encode_frame(_frame(), p.width, p.height, p.jpeg_quality)
        img = cv2.imdecode(np.frombuffer(enc.data, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape[:2] == (p.height, p.width)
        assert enc.encode_ms >= 0


def test_higher_quality_means_more_bytes():
    f = _frame()
    sizes = [len(encode_frame(f, 640, 360, q).data) for q in (30, 60, 90)]
    assert sizes[0] < sizes[1] < sizes[2]


def test_profiles_produce_increasing_sizes():
    profiles, order = load_profiles()
    f = _frame()
    sizes = [len(encode_frame(f, profiles[n].width, profiles[n].height, profiles[n].jpeg_quality).data)
             for n in order]
    assert sizes == sorted(sizes)


def test_non_16x9_source_is_center_cropped_not_stretched():
    src = np.zeros((480, 640, 3), np.uint8)  # 4:3
    out = crop_to_aspect(src, 1280, 720)
    assert out.shape[:2] == (360, 640)
    wide = np.zeros((400, 1600, 3), np.uint8)  # 4:1
    assert crop_to_aspect(wide, 1280, 720).shape[:2] == (400, 711)


def test_16x9_source_untouched_by_crop():
    src = np.zeros((1080, 1920, 3), np.uint8)
    assert crop_to_aspect(src, 640, 360) is src
