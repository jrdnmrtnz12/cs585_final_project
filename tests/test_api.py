"""Edge server API tests, using MockDetector (no model needed)."""
from __future__ import annotations

import socket

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from edge import protocol as P
from edge.server.app import create_app
from edge.server.detector import MockDetector
from edge.server.udp_echo import UdpEcho


def _jpeg(w: int = 320, h: int = 180) -> bytes:
    img = np.random.default_rng(0).integers(0, 255, (h, w, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    assert ok
    return buf.tobytes()


@pytest.fixture()
def client():
    app = create_app(MockDetector(delay_ms=0), udp_port=None)
    with TestClient(app) as c:
        yield c


def test_health_reports_mock(client):
    r = client.get(P.HEALTH_PATH)
    assert r.status_code == 200
    body = P.HealthResponse.model_validate(r.json())
    assert body.mock is True and body.detector == "mock"


def test_infer_valid_jpeg(client):
    r = client.post(P.INFER_PATH, content=_jpeg(320, 180),
                    headers={"Content-Type": P.JPEG_CONTENT_TYPE, P.HDR_FRAME_ID: "42", P.HDR_PROFILE: "low"})
    assert r.status_code == 200
    body = P.InferResponse.model_validate(r.json())
    assert body.frame_id == 42
    assert body.image_wh == [320, 180]
    assert body.mock is True
    assert body.detections == []  # mock never invents boxes
    t = body.timing
    assert t.decode_ms >= 0 and t.infer_ms >= 0 and t.server_total_ms >= t.decode_ms


def test_infer_rejects_garbage(client):
    r = client.post(P.INFER_PATH, content=b"not a jpeg", headers={P.HDR_FRAME_ID: "1"})
    assert r.status_code == 400
    assert "JPEG" in r.json()["detail"]


def test_infer_rejects_missing_frame_id(client):
    r = client.post(P.INFER_PATH, content=_jpeg())
    assert r.status_code == 400


def test_infer_rejects_empty_body(client):
    r = client.post(P.INFER_PATH, content=b"", headers={P.HDR_FRAME_ID: "1"})
    assert r.status_code == 400


def test_stats_endpoints(client):
    assert client.post(P.STATS_RESET_PATH).status_code == 200
    assert "n_samples" in client.get(P.STATS_PATH).json()


def test_udp_echo_roundtrip():
    echo = UdpEcho("127.0.0.1", 0)
    port = echo.start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        pkt = P.pack_probe(7, 123456789)
        assert len(pkt) == P.PROBE_SIZE
        s.sendto(pkt, ("127.0.0.1", port))
        data, _ = s.recvfrom(2048)
        assert data == pkt
        assert P.unpack_probe(data) == (7, 123456789)
        s.close()
    finally:
        echo.stop()
