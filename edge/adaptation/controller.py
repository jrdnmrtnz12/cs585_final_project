"""Profile-selection policies: fixed baselines and the adaptive controller.

The adaptive controller runs once per measurement window. It sees ONLY what a
real deployment could measure (probe RTT and loss, observed upload goodput,
observed response latency, server processing time). It never sees the
scenario label.

Core idea (Kurose & Ross Ch. 1 delay model):

    predicted_latency(p) = base_rtt + 8 * L_p / R + server_time

L_p is the typical JPEG size of profile p in bytes, and R is the observed
upload goodput. The thresholds come from the response-time TARGET instead of
hand-picked RTT/bandwidth cutoffs:

  DOWN one level if any of:
      observed latency median > TARGET              ("latency")
      in-flight request older than TARGET           ("stall")
      predicted_latency(current) > TARGET           ("predicted")
      probe loss > LOSS_HI (if use_loss_signal)     ("loss")
  UP one level if, for `upgrade_windows` consecutive windows, all of:
      at least one response arrived in the window (never upgrade on no data)
      observed latency median < margin * TARGET
      predicted_latency(next) < margin * TARGET, or goodput unknown ("probe")
      probe loss < LOSS_LO (if use_loss_signal)
    and the upgrade cooldown has expired.

Hysteresis comes from the gap between margin*TARGET (to go up) and TARGET (to
go down) plus the consecutive-window requirement. If an upgrade is followed by
a downgrade within the probe period, the upgrade "failed" and the upgrade
cooldown doubles (up to max_cooldown_s). It resets to the base value after an
upgrade that holds. This is application-level adaptation of how much data we
generate. It is NOT TCP congestion control, which separately adjusts how fast
the transport sends the bytes we give it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from edge.config import ControllerConfig


@dataclass(frozen=True)
class WindowStats:
    t_s: float                         # monotonic seconds since trial start (end of window)
    rtt_ms: float | None = None        # base RTT: minimum probe RTT (propagation + added delay)
    loss_pct: float | None = None      # UDP probe loss in the window
    goodput_mbps: float | None = None  # observed upload goodput (None = unknown / not the bottleneck)
    latency_ms_median: float | None = None
    n_responses: int = 0
    server_ms: float | None = None     # median server processing time in the window
    oldest_inflight_ms: float | None = None


@dataclass(frozen=True)
class Decision:
    profile: str
    changed: bool
    reason: str
    predicted_ms: float | None = None


class FixedPolicy:
    """Baseline: one profile for the whole trial, whatever the network does."""

    def __init__(self, profile: str):
        self.profile = profile

    def observe_frame_size(self, profile: str, nbytes: int) -> None:
        pass

    def step(self, stats: WindowStats) -> Decision:
        return Decision(self.profile, False, "fixed")


@dataclass
class _State:
    idx: int
    good_streak: int = 0
    last_change_t: float = float("-inf")
    last_dir: str = ""                     # "up" | "down" | ""
    next_up_allowed_t: float = 0.0
    next_down_allowed_t: float = 0.0
    up_cooldown_s: float = 0.0
    last_server_ms: float = 0.0
    sizes: dict[str, float] = field(default_factory=dict)


class AdaptiveController:
    def __init__(self, ladder: list[str], est_bytes: dict[str, float], cfg: ControllerConfig):
        if cfg.start_profile not in ladder:
            raise ValueError(f"start_profile {cfg.start_profile!r} not in ladder {ladder}")
        self.ladder = list(ladder)
        self.cfg = cfg
        self.s = _State(idx=self.ladder.index(cfg.start_profile), up_cooldown_s=cfg.cooldown_s,
                        sizes={p: float(est_bytes[p]) for p in ladder})

    @property
    def profile(self) -> str:
        return self.ladder[self.s.idx]

    # ---- measurements -------------------------------------------------
    def observe_frame_size(self, profile: str, nbytes: int) -> None:
        """EWMA of the JPEG size actually produced at each profile."""
        a = self.cfg.size_ewma_alpha
        self.s.sizes[profile] = (1 - a) * self.s.sizes[profile] + a * nbytes

    def predict_ms(self, profile: str, stats: WindowStats) -> float | None:
        """Predicted response latency for `profile`; None if RTT or goodput is unknown."""
        if stats.rtt_ms is None or not stats.goodput_mbps:
            return None
        transfer_ms = self.s.sizes[profile] * 8 / (stats.goodput_mbps * 1e6) * 1e3
        server_ms = stats.server_ms if stats.server_ms is not None else self.s.last_server_ms
        return stats.rtt_ms + transfer_ms + server_ms

    # ---- decision -----------------------------------------------------
    def step(self, st: WindowStats) -> Decision:
        cfg, s, t = self.cfg, self.s, st.t_s
        if st.server_ms is not None:
            s.last_server_ms = st.server_ms
        cur = self.profile
        probe_period_s = cfg.upgrade_windows * cfg.window_s

        # An upgrade that survived a full probe period succeeded: reset the backoff.
        if s.last_dir == "up" and t - s.last_change_t > probe_period_s:
            s.up_cooldown_s = cfg.cooldown_s

        pred_cur = self.predict_ms(cur, st)
        down = []
        if st.n_responses > 0 and st.latency_ms_median is not None and st.latency_ms_median > cfg.target_ms:
            down.append("latency")
        if st.oldest_inflight_ms is not None and st.oldest_inflight_ms > cfg.target_ms:
            down.append("stall")
        if pred_cur is not None and pred_cur > cfg.target_ms:
            down.append("predicted")
        if cfg.use_loss_signal and st.loss_pct is not None and st.loss_pct > cfg.loss_hi_pct:
            down.append("loss")

        if down:
            s.good_streak = 0
            if s.idx > 0 and t >= s.next_down_allowed_t:
                if s.last_dir == "up" and t - s.last_change_t <= probe_period_s:
                    s.up_cooldown_s = min(2 * s.up_cooldown_s, cfg.max_cooldown_s)  # failed probe
                return self._change(-1, t, "+".join(down), pred_cur)
            return Decision(cur, False, "hold:" + "+".join(down), pred_cur)

        if s.idx == len(self.ladder) - 1:
            s.good_streak = 0
            return Decision(cur, False, "hold:top", pred_cur)

        nxt = self.ladder[s.idx + 1]
        pred_nxt = self.predict_ms(nxt, st)
        bound = cfg.upgrade_margin * cfg.target_ms
        good = (
            st.n_responses > 0
            and st.latency_ms_median is not None and st.latency_ms_median < bound
            and (pred_nxt is None or pred_nxt < bound)
            and (not cfg.use_loss_signal or (st.loss_pct is not None and st.loss_pct < cfg.loss_lo_pct))
        )
        s.good_streak = s.good_streak + 1 if good else 0
        if good and s.good_streak >= cfg.upgrade_windows and t >= s.next_up_allowed_t:
            return self._change(+1, t, "predicted_ok" if pred_nxt is not None else "probe", pred_nxt)
        return Decision(cur, False, "hold", pred_cur)

    def _change(self, direction: int, t: float, reason: str, predicted: float | None) -> Decision:
        s, cfg = self.s, self.cfg
        s.idx += direction
        s.good_streak = 0
        s.last_change_t = t
        s.last_dir = "up" if direction > 0 else "down"
        s.next_up_allowed_t = t + (s.up_cooldown_s if direction > 0 else max(cfg.cooldown_s, s.up_cooldown_s))
        # After an upgrade we may fall back immediately; after a downgrade, wait for it to take effect.
        s.next_down_allowed_t = t if direction > 0 else t + cfg.cooldown_s
        return Decision(self.profile, True, reason, predicted)
