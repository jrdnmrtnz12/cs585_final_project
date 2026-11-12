"""Adaptive controller behavior. Pure logic: no network, no clock."""
from __future__ import annotations

from dataclasses import replace

import pytest

from edge.adaptation.controller import AdaptiveController, FixedPolicy, WindowStats
from edge.config import ControllerConfig

LADDER = ["low", "medium", "high"]
SIZES = {"low": 6_000, "medium": 25_000, "high": 120_000}
CFG = ControllerConfig(target_ms=300, upgrade_margin=0.7, upgrade_windows=3, cooldown_s=2.0,
                       max_cooldown_s=16.0, loss_hi_pct=2.0, loss_lo_pct=0.5, start_profile="medium")

# Good network: 5 ms RTT, 100 Mbit/s goodput -> high predicted ~5 + 9.6 + 10 = 25 ms
GOOD = WindowStats(t_s=0, rtt_ms=5, loss_pct=0.0, goodput_mbps=100, latency_ms_median=30,
                   n_responses=10, server_ms=10)
# Poor network: 80 ms RTT, 2 Mbit/s -> medium predicted ~80 + 100 + 10 = 190 ms, high ~570 ms
POOR = WindowStats(t_s=0, rtt_ms=80, loss_pct=3.0, goodput_mbps=2, latency_ms_median=450,
                   n_responses=3, server_ms=10)


def make(cfg: ControllerConfig = CFG) -> AdaptiveController:
    return AdaptiveController(LADDER, SIZES, cfg)


def run(ctrl, stats: WindowStats, t0: float, n: int):
    return [ctrl.step(replace(stats, t_s=t0 + i)) for i in range(n)]


def test_starts_at_configured_profile():
    assert make().profile == "medium"


def test_poor_conditions_downgrade():
    c = make()
    d = c.step(replace(POOR, t_s=1))
    assert d.changed and c.profile == "low"
    assert "latency" in d.reason and "loss" in d.reason


def test_upgrade_requires_consecutive_good_windows():
    c = make()
    ds = run(c, GOOD, t0=1, n=3)
    assert [d.changed for d in ds] == [False, False, True]
    assert c.profile == "high"


def test_one_bad_window_resets_good_streak():
    c = make()
    c.step(replace(GOOD, t_s=1))
    c.step(replace(GOOD, t_s=2))
    # borderline window: not bad enough to downgrade, not good enough to count
    c.step(replace(GOOD, t_s=3, latency_ms_median=250))
    assert c.profile == "medium"
    ds = run(c, GOOD, t0=4, n=3)
    assert ds[-1].changed and c.profile == "high"


def test_only_one_level_per_step_and_bounds():
    c = make()
    run(c, GOOD, t0=1, n=20)
    assert c.profile == "high"          # never beyond top
    d = c.step(replace(POOR, t_s=30))
    assert c.profile == "medium"         # one level down, not two
    run(c, POOR, t0=40, n=20)
    assert c.profile == "low"            # never below bottom


def test_downgrade_cooldown_prevents_cascade():
    c = make(replace(CFG, start_profile="high"))
    c.step(replace(POOR, t_s=1))
    assert c.profile == "medium"
    c.step(replace(POOR, t_s=2))         # within 2 s cooldown of the last downgrade
    assert c.profile == "medium"
    c.step(replace(POOR, t_s=3))
    assert c.profile == "low"


def test_upgrade_cooldown_respected():
    c = make(replace(CFG, start_profile="low"))
    run(c, GOOD, t0=1, n=3)
    assert c.profile == "medium"         # upgraded at t=3
    run(c, GOOD, t0=4, n=1)              # t=4: streak restarted and cooldown active
    assert c.profile == "medium"
    run(c, GOOD, t0=5, n=2)              # t=6: 3 good windows and cooldown (until t=5) over
    assert c.profile == "high"


def test_failed_probe_doubles_upgrade_cooldown():
    c = make(replace(CFG, start_profile="low"))
    run(c, GOOD, t0=1, n=3)                      # up to medium at t=3
    assert c.profile == "medium"
    c.step(replace(POOR, t_s=4))                  # immediate fallback: probe failed
    assert c.profile == "low"
    assert c.s.up_cooldown_s == 4.0
    c.step(replace(POOR, t_s=5))
    assert c.s.up_cooldown_s == 4.0               # unchanged while holding at the bottom


def test_borderline_conditions_do_not_oscillate():
    """Latency between 0.7*target and target: neither up nor down, ever."""
    c = make()
    border = replace(GOOD, latency_ms_median=250, goodput_mbps=8)  # medium predicted ~5+25+10=40, high ~135
    ds = run(c, border, t0=1, n=60)
    assert not any(d.changed for d in ds)
    assert c.profile == "medium"


def test_missing_data_holds():
    c = make()
    empty = WindowStats(t_s=0)
    ds = run(c, empty, t0=1, n=10)
    assert not any(d.changed for d in ds)
    assert c.profile == "medium"


def test_stall_triggers_downgrade():
    c = make()
    stalled = WindowStats(t_s=1, rtt_ms=5, loss_pct=0, n_responses=0, oldest_inflight_ms=900)
    d = c.step(stalled)
    assert d.changed and d.reason == "stall" and c.profile == "low"


def test_prediction_alone_triggers_downgrade():
    c = make(replace(CFG, start_profile="high"))
    # latency so far fine, but at 2 Mbit/s a 120 kB frame needs ~480 ms of transfer
    s = replace(GOOD, t_s=1, goodput_mbps=2.0, latency_ms_median=100)
    d = c.step(s)
    assert d.changed and "predicted" in d.reason


def test_loss_signal_can_be_disabled():
    lossy = replace(GOOD, loss_pct=5.0)
    with_loss = make()
    assert with_loss.step(replace(lossy, t_s=1)).changed
    without = make(replace(CFG, use_loss_signal=False))
    ds = run(without, lossy, t0=1, n=3)
    assert without.profile == "high"  # loss ignored entirely in the ablation


def test_unknown_goodput_allows_probe_up():
    c = make(replace(CFG, start_profile="low"))
    s = replace(GOOD, goodput_mbps=None)
    ds = run(c, s, t0=1, n=3)
    assert ds[-1].changed and ds[-1].reason == "probe"


def test_frame_size_ewma_updates_prediction():
    c = make()
    s = replace(GOOD, goodput_mbps=10)
    before = c.predict_ms("medium", s)
    for _ in range(50):
        c.observe_frame_size("medium", 100_000)
    assert c.predict_ms("medium", s) > before


def test_fixed_policy_never_changes():
    f = FixedPolicy("high")
    for st in (GOOD, POOR, WindowStats(t_s=0)):
        d = f.step(st)
        assert not d.changed and d.profile == "high"


def test_bad_start_profile_rejected():
    with pytest.raises(ValueError):
        AdaptiveController(LADDER, SIZES, replace(CFG, start_profile="ultra"))
