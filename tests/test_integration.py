"""End-to-end: real HTTP server (mock detector) + real client session over loopback."""
from __future__ import annotations

import csv
import socket
import threading
import time
from dataclasses import replace

import uvicorn

from edge.adaptation.controller import AdaptiveController, FixedPolicy
from edge.client.capture import SyntheticSource
from edge.client.session import ClientSession
from edge.config import load_experiment, load_profiles
from edge.server.app import create_app
from edge.server.detector import MockDetector


def _free_port(kind: int) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    def __init__(self) -> None:
        self.http_port = _free_port(socket.SOCK_STREAM)
        self.udp_port = _free_port(socket.SOCK_DGRAM)
        app = create_app(MockDetector(delay_ms=5), udp_port=self.udp_port, udp_host="127.0.0.1")
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.http_port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            assert time.time() < deadline, "server did not start"
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def _rows(path) -> list[dict[str, str]]:
    return list(csv.DictReader(open(path)))


def _run(tmp_path, policy, duration=3.0):
    profiles, ladder = load_profiles()
    with _Server() as srv:
        exp = replace(load_experiment(), http_port=srv.http_port, udp_port=srv.udp_port, request_timeout_s=2.0)
        sess = ClientSession(exp=exp, profiles=profiles, policy=policy, trial_id="t_int",
                             trial_dir=tmp_path, source=SyntheticSource(), server_host="127.0.0.1")
        return sess.run(duration)


def test_fixed_low_end_to_end(tmp_path):
    result = _run(tmp_path, FixedPolicy("low"))
    assert result["status"] == "complete"
    frames = _rows(tmp_path / "frames.csv")
    assert len(frames) >= 8                          # ~5 fps for 3 s
    assert all(f["outcome"] == "ok" for f in frames)
    assert {f["profile"] for f in frames} == {"low"}  # a fixed strategy never adapts
    ids = [int(f["frame_id"]) for f in frames]
    assert len(ids) == len(set(ids)) and ids == sorted(ids)
    for f in frames:
        capture_ns, send_ns, recv_ns = (int(f[k]) for k in ("capture_ns", "send_ns", "recv_ns"))
        assert capture_ns <= send_ns < recv_ns
        # Assert positivity on the raw clock values. Rounded CSV milliseconds
        # alone can hide sub-millisecond intervals.
        assert float(f["latency_ms"]) >= float(f["request_rtt_ms"]) >= 0
        assert (f["width_px"], f["height_px"]) == ("320", "180")
    net = _rows(tmp_path / "netmon.csv")
    assert len(net) >= 2
    assert any(r["rtt_ms_median"] for r in net)
    assert not _rows(tmp_path / "switches.csv")


def test_adaptive_end_to_end_logs_switches(tmp_path):
    profiles, ladder = load_profiles()
    exp = load_experiment()
    ctrl = AdaptiveController(ladder, {n: p.est_bytes for n, p in profiles.items()},
                              replace(exp.controller, start_profile="low", use_loss_signal=False))
    result = _run(tmp_path, ctrl, duration=6.0)
    assert result["status"] == "complete"
    switches = _rows(tmp_path / "switches.csv")
    # loopback is fast: the controller should climb from low at least once
    assert switches and switches[0]["from_profile"] == "low" and switches[0]["to_profile"] == "medium"
    profiles_seen = {f["profile"] for f in _rows(tmp_path / "frames.csv")}
    assert {"low", "medium"} <= profiles_seen
