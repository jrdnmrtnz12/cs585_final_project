"""Client building blocks: latest-frame slot, goodput estimator, UDP prober."""
from __future__ import annotations

import time

import numpy as np

from edge.client.netmon import GoodputEstimator, UdpProber
from edge.client.sender import LatestFrameSlot, SlotItem
from edge.server.udp_echo import UdpEcho


def _item(i: int) -> SlotItem:
    return SlotItem(i, i * 1000, np.zeros((2, 2, 3), np.uint8))


def test_slot_is_bounded_and_reports_superseded():
    slot = LatestFrameSlot()
    assert slot.put(_item(1)) is None
    assert slot.put(_item(2)) == 1        # frame 1 replaced before it was sent
    assert slot.put(_item(3)) == 2
    got = slot.take(timeout=0.1)
    assert got is not None and got.frame_id == 3  # always the newest frame
    assert slot.take(timeout=0.05) is None        # nothing queued behind it


def test_slot_close_unblocks_take():
    slot = LatestFrameSlot()
    slot.close()
    t = time.monotonic()
    assert slot.take(timeout=5) is None
    assert time.monotonic() - t < 1


def test_goodput_estimate():
    g = GoodputEstimator(alpha=1.0, min_transfer_ms=5)
    # 125 kB in 100 ms of transfer time -> 10 Mbit/s
    s = g.add(125_000, request_rtt_ms=130, server_ms=10, base_rtt_ms=20)
    assert abs(s - 10.0) < 1e-6
    assert g.close_window() == s


def test_goodput_unknown_when_transfers_too_fast():
    g = GoodputEstimator(alpha=1.0, min_transfer_ms=5)
    g.add(125_000, request_rtt_ms=130, server_ms=10, base_rtt_ms=20)
    g.close_window()
    assert g.add(5_000, request_rtt_ms=32, server_ms=10, base_rtt_ms=20) is None  # 2 ms transfer
    assert g.close_window() is None   # link not the bottleneck at this size: unknown, not stale


def test_goodput_needs_base_rtt():
    assert GoodputEstimator().add(10_000, 50, 5, None) is None


def test_prober_measures_rtt_and_no_loss_on_loopback():
    echo = UdpEcho("127.0.0.1", 0)
    port = echo.start()
    prober = UdpProber("127.0.0.1", port, probe_hz=50, timeout_s=0.3)
    try:
        prober.start()
        time.sleep(1.0)
        w = prober.window(time.monotonic_ns(), int(0.5e9))
        assert w["rtt_ms_median"] is not None and w["rtt_ms_median"] < 50
        assert w["probes_sent"] > 0 and w["probes_lost"] == 0 and w["loss_pct"] == 0.0
    finally:
        prober.stop()
        echo.stop()


def test_prober_counts_loss_when_nothing_answers():
    prober = UdpProber("127.0.0.1", 9, probe_hz=50, timeout_s=0.2)  # port 9 (discard): no echo
    try:
        prober.start()
        time.sleep(0.8)
        w = prober.window(time.monotonic_ns(), int(0.4e9))
        assert w["probes_sent"] > 0 and w["loss_pct"] == 100.0
    finally:
        prober.stop()
