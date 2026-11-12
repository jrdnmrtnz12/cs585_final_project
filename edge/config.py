"""Load and validate the YAML configuration in config/.

Every value is checked at startup so that a bad config fails immediately,
not halfway through a trial. Run as a module to print a scenario in a form
netem/netem.sh can read:

    python -m edge.config --scenario poor      ->  "80 3 2"
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"

# netem has no "uncapped" rate. Sending an explicit, very high rate keeps
# `tc qdisc change` well defined: we always pass delay, loss AND rate, so no
# option from the previous scenario can carry over.
UNCAPPED_RATE_MBIT = 1000.0


class ConfigError(ValueError):
    """Raised when a configuration value is missing or out of range."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


@dataclass(frozen=True)
class Profile:
    name: str
    width: int
    height: int
    jpeg_quality: int
    fps: float
    est_bytes: float

    def validate(self) -> None:
        _require(self.width > 0 and self.height > 0, f"{self.name}: width/height must be positive")
        _require(abs(self.width / self.height - 16 / 9) < 0.01,
                 f"{self.name}: {self.width}x{self.height} is not 16:9")
        _require(1 <= self.jpeg_quality <= 100, f"{self.name}: jpeg_quality must be 1-100")
        _require(0 < self.fps <= 60, f"{self.name}: fps must be in (0, 60]")
        _require(self.est_bytes > 0, f"{self.name}: est_bytes must be positive")


@dataclass(frozen=True)
class Scenario:
    name: str
    delay_ms: float
    loss_pct: float
    rate_mbit: float | None

    def validate(self) -> None:
        _require(0 <= self.delay_ms <= 2000, f"{self.name}: delay_ms out of range")
        _require(0 <= self.loss_pct < 100, f"{self.name}: loss_pct out of range")
        _require(self.rate_mbit is None or self.rate_mbit > 0, f"{self.name}: rate_mbit must be > 0 or null")

    @property
    def netem_rate_mbit(self) -> float:
        return UNCAPPED_RATE_MBIT if self.rate_mbit is None else self.rate_mbit


@dataclass(frozen=True)
class ScheduleStep:
    at_s: float
    scenario: str


@dataclass(frozen=True)
class Schedule:
    name: str
    duration_s: float
    steps: tuple[ScheduleStep, ...]


@dataclass(frozen=True)
class ControllerConfig:
    target_ms: float = 300.0
    stale_factor: float = 2.0
    window_s: float = 1.0
    upgrade_margin: float = 0.7
    upgrade_windows: int = 3
    cooldown_s: float = 2.0
    max_cooldown_s: float = 16.0
    loss_hi_pct: float = 2.0
    loss_lo_pct: float = 0.5
    use_loss_signal: bool = True
    start_profile: str = "medium"
    size_ewma_alpha: float = 0.2

    def validate(self) -> None:
        _require(self.target_ms > 0, "controller.target_ms must be positive")
        _require(0 < self.upgrade_margin < 1, "controller.upgrade_margin must be in (0, 1)")
        _require(self.upgrade_windows >= 1, "controller.upgrade_windows must be >= 1")
        _require(0 < self.cooldown_s <= self.max_cooldown_s, "controller cooldowns invalid")
        _require(0 <= self.loss_lo_pct < self.loss_hi_pct, "need 0 <= loss_lo_pct < loss_hi_pct")
        _require(0 < self.size_ewma_alpha <= 1, "controller.size_ewma_alpha must be in (0, 1]")


@dataclass(frozen=True)
class NetmonConfig:
    probe_hz: float = 20.0
    probe_timeout_s: float = 1.0
    goodput_ewma_alpha: float = 0.3
    min_transfer_ms: float = 5.0

    def validate(self) -> None:
        _require(0 < self.probe_hz <= 200, "netmon.probe_hz must be in (0, 200]")
        _require(self.probe_timeout_s > 0, "netmon.probe_timeout_s must be positive")
        _require(0 < self.goodput_ewma_alpha <= 1, "netmon.goodput_ewma_alpha must be in (0, 1]")


@dataclass(frozen=True)
class ExperimentConfig:
    server_host: str
    http_port: int
    udp_port: int
    source: str
    video_path: str
    monitor: int
    screen_region: dict[str, int] | None
    request_timeout_s: float
    duration_s: float
    warmup_s: float
    results_dir: str
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    netmon: NetmonConfig = field(default_factory=NetmonConfig)

    def validate(self) -> None:
        _require(self.source in ("screen", "video", "synthetic"), "client.source must be screen|video|synthetic")
        _require(0 < self.http_port < 65536 and 0 < self.udp_port < 65536, "ports out of range")
        _require(self.duration_s > self.warmup_s >= 0, "trial.duration_s must exceed warmup_s")
        _require(self.request_timeout_s > 0, "client.request_timeout_s must be positive")
        self.controller.validate()
        self.netmon.validate()


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data = yaml.safe_load(f)
    _require(isinstance(data, dict), f"{path} must contain a YAML mapping")
    return data


def load_profiles(path: Path = CONFIG_DIR / "profiles.yaml") -> tuple[dict[str, Profile], list[str]]:
    """Return (profiles by name, ladder order lowest to highest)."""
    data = _load_yaml(path)
    profiles = {name: Profile(name=name, **vals) for name, vals in data["profiles"].items()}
    for p in profiles.values():
        p.validate()
    order = list(data["order"])
    _require(set(order) == set(profiles), "profiles.yaml: 'order' must list every profile exactly once")
    sizes = [profiles[n].width * profiles[n].height for n in order]
    _require(sizes == sorted(sizes), "profiles.yaml: 'order' must go from lowest to highest resolution")
    return profiles, order


def load_scenarios(path: Path = CONFIG_DIR / "scenarios.yaml") -> tuple[dict[str, Scenario], dict[str, Schedule]]:
    data = _load_yaml(path)
    scenarios = {name: Scenario(name=name, **vals) for name, vals in data["scenarios"].items()}
    for s in scenarios.values():
        s.validate()
    schedules: dict[str, Schedule] = {}
    for name, vals in (data.get("schedules") or {}).items():
        steps = tuple(ScheduleStep(float(s["at_s"]), str(s["scenario"])) for s in vals["steps"])
        _require(steps and steps[0].at_s == 0, f"schedule {name}: first step must be at_s 0")
        _require(all(a.at_s < b.at_s for a, b in zip(steps, steps[1:])), f"schedule {name}: steps must be increasing")
        _require(all(s.scenario in scenarios for s in steps), f"schedule {name}: unknown scenario")
        _require(float(vals["duration_s"]) > steps[-1].at_s, f"schedule {name}: duration_s too short")
        _require(name not in scenarios, f"schedule {name} has the same name as a scenario")
        schedules[name] = Schedule(name, float(vals["duration_s"]), steps)
    return scenarios, schedules


def load_experiment(path: Path = CONFIG_DIR / "experiment.yaml", **overrides: Any) -> ExperimentConfig:
    """Load experiment.yaml. Keyword overrides replace top-level fields (None is ignored)."""
    d = _load_yaml(path)
    cfg = dict(
        server_host=d["server"]["host"],
        http_port=int(d["server"]["http_port"]),
        udp_port=int(d["server"]["udp_port"]),
        source=d["client"]["source"],
        video_path=d["client"]["video_path"],
        monitor=int(d["client"].get("monitor", 1)),
        screen_region=d["client"].get("screen_region"),
        request_timeout_s=float(d["client"]["request_timeout_s"]),
        duration_s=float(d["trial"]["duration_s"]),
        warmup_s=float(d["trial"]["warmup_s"]),
        results_dir=d["trial"]["results_dir"],
        controller=ControllerConfig(**d.get("controller", {})),
        netmon=NetmonConfig(**d.get("netmon", {})),
    )
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    exp = ExperimentConfig(**cfg)
    exp.validate()
    return exp


def to_jsonable(obj: Any) -> Any:
    """Dataclass (or nested structure of them) to plain JSON-compatible data."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print config values for shell scripts.")
    ap.add_argument("--scenario", required=True, help="scenario name from config/scenarios.yaml")
    args = ap.parse_args(argv)
    scenarios, _ = load_scenarios()
    if args.scenario not in scenarios:
        print(f"unknown scenario '{args.scenario}'; known: {', '.join(scenarios)}", file=sys.stderr)
        return 2
    s = scenarios[args.scenario]
    print(f"{s.delay_ms:g} {s.loss_pct:g} {s.netem_rate_mbit:g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
