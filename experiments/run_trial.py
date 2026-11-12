"""Run ONE trial: (strategy, network scenario, repetition) -> results/raw/<trial_id>/.

Local development (no netem, mock or real server on this machine):
    python experiments/run_trial.py --strategy adaptive --scenario none --duration 15 --allow-mock

Testbed (Linux client, real impairment, screen capture):
    NETEM_IF=wlan0 EDGE_SERVER=10.58.5.10 \\
    python experiments/run_trial.py --strategy fixed_high --scenario poor --rep 1 --source screen --label pilot

Steps: health check -> apply scenario -> reset server stats -> run session
(dynamic schedules change netem on a timer) -> ALWAYS reset netem -> write
metadata.json -> completeness check. Exit code 0 only if the trial is complete.
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import logging
import os
import platform
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx

from edge import protocol as P
from edge.adaptation.controller import AdaptiveController, FixedPolicy
from edge.client.capture import make_source
from edge.client.session import ClientSession
from edge.config import REPO_ROOT, load_experiment, load_profiles, load_scenarios, to_jsonable
from edge.logging_utils import make_trial_id, new_trial_dir, write_json

log = logging.getLogger("run_trial")
STRATEGIES = ("fixed_high", "fixed_low", "adaptive")
NETEM_SH = REPO_ROOT / "netem" / "netem.sh"


class Netem:
    """Thin wrapper around netem/netem.sh that records every command it runs."""

    def __init__(self, enabled: bool, dry_run: bool, interface: str | None, server_ip: str):
        self.enabled, self.dry_run, self.interface, self.server_ip = enabled, dry_run, interface, server_ip
        self.history: list[dict[str, str]] = []
        self.applied = False

    def _run(self, *args: str) -> None:
        if not self.enabled:
            return
        cmd = [str(NETEM_SH), *args] + (["--dry-run"] if self.dry_run else [])
        env = {**os.environ, "NETEM_IF": self.interface or "", "SERVER_IP": self.server_ip}
        out = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.history.append({"at": datetime.now(timezone.utc).isoformat(), "cmd": " ".join(args),
                             "stdout": out.stdout, "stderr": out.stderr, "rc": str(out.returncode)})
        if out.returncode != 0:
            raise RuntimeError(f"netem {' '.join(args)} failed: {out.stderr.strip()}")
        log.info("netem %s\n%s", " ".join(args), out.stdout.strip())

    def apply(self, scenario: str) -> None:
        self._run("apply", scenario)
        self.applied = True

    def change(self, scenario: str) -> None:
        self._run("change", scenario)

    def reset(self) -> None:
        if self.applied:
            self._run("reset")
            self.applied = False


def _git_info() -> dict[str, object]:
    def git(*a: str) -> str | None:
        r = subprocess.run(["git", *a], cwd=REPO_ROOT, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    return {"commit": git("rev-parse", "--verify", "-q", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def _versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for pkg in ("numpy", "opencv-python", "httpx", "mss", "pydantic", "psutil", "pyyaml"):
        try:
            out[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            out[pkg] = None
    return out


def main(argv: list[str] | None = None) -> int:
    scenarios, schedules = load_scenarios()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", choices=STRATEGIES, required=True)
    ap.add_argument("--scenario", choices=["none", *scenarios, *schedules], required=True,
                    help="'none' = no impairment (local development)")
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--duration", type=float, help="seconds; default from config (schedules use their own)")
    ap.add_argument("--server", default=os.environ.get("EDGE_SERVER"))
    ap.add_argument("--source", choices=["screen", "video", "synthetic"])
    ap.add_argument("--video")
    ap.add_argument("--trial-id")
    ap.add_argument("--results-dir")
    ap.add_argument("--label", choices=["dev", "pilot", "final"], default="dev",
                    help="what kind of data this is; pilot/final require real detection + screen capture")
    ap.add_argument("--allow-mock", action="store_true", help="permit a mock-detector server (dev only)")
    ap.add_argument("--netem-if", default=os.environ.get("NETEM_IF"))
    ap.add_argument("--netem-dry-run", action="store_true", help="print tc commands instead of running them")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per frame is too noisy

    exp = load_experiment(server_host=args.server, source=args.source, video_path=args.video,
                          results_dir=args.results_dir)
    profiles, ladder = load_profiles()
    schedule = schedules.get(args.scenario)
    duration = args.duration or (schedule.duration_s if schedule else exp.duration_s)

    # ---- health + integrity guards ------------------------------------
    base = f"http://{exp.server_host}:{exp.http_port}"
    try:
        health = P.HealthResponse.model_validate(httpx.get(base + P.HEALTH_PATH, timeout=5).json())
    except Exception as e:
        log.error("edge server not reachable at %s: %s", base, e)
        return 2
    if health.mock and not args.allow_mock:
        log.error("server is running the MOCK detector; pass --allow-mock for development runs")
        return 2
    if args.label in ("pilot", "final") and (health.mock or exp.source != "screen"):
        log.error("pilot/final trials require real detection and --source screen")
        return 2

    netem_on = args.scenario != "none"
    if netem_on and not args.netem_dry_run and (platform.system() != "Linux" or not args.netem_if):
        log.error("scenario %s needs Linux tc/netem and --netem-if/NETEM_IF (or --netem-dry-run)", args.scenario)
        return 2

    trial_id = args.trial_id or make_trial_id(args.strategy, args.scenario, args.rep)
    trial_dir = new_trial_dir(REPO_ROOT / exp.results_dir if not Path(exp.results_dir).is_absolute()
                              else exp.results_dir, trial_id)

    if args.strategy == "adaptive":
        policy = AdaptiveController(ladder, {n: p.est_bytes for n, p in profiles.items()}, exp.controller)
    else:
        policy = FixedPolicy("high" if args.strategy == "fixed_high" else "low")

    netem = Netem(netem_on, args.netem_dry_run, args.netem_if, exp.server_host)
    meta: dict[str, object] = {
        "trial_id": trial_id, "label": args.label, "strategy": args.strategy, "scenario": args.scenario,
        "rep": args.rep, "started_at": datetime.now(timezone.utc).isoformat(), "duration_s": duration,
        "warmup_s": exp.warmup_s, "target_ms": exp.controller.target_ms,
        "stale_ms": exp.controller.target_ms * exp.controller.stale_factor,
        "source": {"kind": exp.source, "video_path": exp.video_path if exp.source == "video" else None,
                   "monitor": exp.monitor, "region": exp.screen_region},
        "server": {"base_url": base, "health": health.model_dump()},
        "config": {"profiles": to_jsonable(profiles), "ladder": ladder,
                   "scenario": to_jsonable(scenarios.get(args.scenario) or schedule),
                   "scenario_values": to_jsonable(scenarios) if schedule else None,
                   "controller": to_jsonable(exp.controller), "netmon": to_jsonable(exp.netmon)},
        "host": {"platform": platform.platform(), "python": platform.python_version(), "packages": _versions()},
        "git": _git_info(),
    }

    def _sigterm(*_: object) -> None:
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, _sigterm)

    result: dict[str, object] = {"status": "not_started"}
    session: ClientSession | None = None
    stop_sched = threading.Event()
    try:
        if netem_on:
            netem.apply(schedule.steps[0].scenario if schedule else args.scenario)
        httpx.post(base + P.STATS_RESET_PATH, timeout=5)
        source = make_source(exp.source, exp.video_path, exp.monitor, exp.screen_region)
        session = ClientSession(exp=exp, profiles=profiles, policy=policy, trial_id=trial_id,
                                trial_dir=trial_dir, source=source, server_host=exp.server_host)
        session.log_event("scenario", schedule.steps[0].scenario if schedule else args.scenario)

        def scheduler() -> None:
            assert schedule is not None and session is not None
            for step in schedule.steps[1:]:
                while session.clock_ns() / 1e9 < step.at_s:
                    if stop_sched.wait(0.01):
                        return
                try:
                    t_req = session.clock_ns()
                    netem.change(step.scenario)  # takes effect somewhere in [t_req, now]
                    session.log_event("scenario_change", f"{step.scenario};requested_ns={t_req}")
                except Exception as e:  # make the failure visible in the data
                    session.log_event("netem_error", str(e))
        if schedule:
            threading.Thread(target=scheduler, name="schedule", daemon=True).start()
        result = session.run(duration)
    except KeyboardInterrupt:
        result = {**result, "status": "interrupted"}
        log.warning("interrupted")
    except Exception as e:
        result = {**result, "status": "error", "error": f"{type(e).__name__}: {e}"}
        log.exception("trial failed")
    finally:
        stop_sched.set()
        try:
            netem.reset()
        except Exception as e:
            log.error("NETEM RESET FAILED, fix the interface manually: %s", e)
            result["netem_reset_error"] = str(e)
        try:
            meta["server_resources"] = httpx.get(base + P.STATS_PATH, timeout=5).json()
        except Exception:
            meta["server_resources"] = None
        meta["netem"] = {"enabled": netem_on, "dry_run": args.netem_dry_run, "interface": args.netem_if,
                         "commands": netem.history}
        meta["result"] = result
        meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        meta["completeness"] = _completeness(trial_dir, duration, exp.controller.window_s, result)
        write_json(trial_dir / "metadata.json", meta)

    ok = meta["completeness"]["ok"]  # type: ignore[index]
    log.info("trial %s: %s, %s", trial_id, result.get("status"),
             {k: result.get(k) for k in ("sent", "ok", "timeout", "superseded")})
    log.info("saved %s (complete=%s)", trial_dir, ok)
    return 0 if ok else 1


def _completeness(trial_dir: Path, duration: float, window_s: float, result: dict[str, object]) -> dict[str, object]:
    def rows(name: str) -> int:
        p = trial_dir / name
        return max(0, sum(1 for _ in open(p)) - 1) if p.exists() else 0
    frames, netmon = rows("frames.csv"), rows("netmon.csv")
    expected = int(duration / window_s)
    ok = result.get("status") == "complete" and frames > 0 and netmon >= expected - 2
    return {"ok": ok, "frames_rows": frames, "netmon_rows": netmon, "expected_windows": expected}


if __name__ == "__main__":
    sys.exit(main())
