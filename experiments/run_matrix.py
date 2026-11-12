"""Run the full experiment matrix in a randomized, resumable order.

    EDGE_SERVER=10.58.5.10 NETEM_IF=wlan0 \\
    python experiments/run_matrix.py --name final1 --reps 3 --source screen --label final

Trials go to results/raw/<name>/<name>_<strategy>_<scenario>_r<rep>/. The
shuffled order (fixed --seed) is saved once to matrix_plan.json, so a resumed
run keeps the same order. Completed trials are skipped. An incomplete trial
directory is MOVED to <name>/_incomplete/ (never deleted) and the trial reruns.
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_TRIAL = REPO_ROOT / "experiments" / "run_trial.py"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--strategies", nargs="+", default=["fixed_high", "fixed_low", "adaptive"])
    ap.add_argument("--scenarios", nargs="+", default=["good", "moderate", "poor", "dynamic"])
    ap.add_argument("--seed", type=int, default=585)
    ap.add_argument("--gap-s", type=float, default=5.0, help="pause between trials so queues drain")
    ap.add_argument("--dry-list", action="store_true", help="print the order and exit")
    args, passthrough = ap.parse_known_args(argv)  # remaining flags go to run_trial.py

    out = REPO_ROOT / "results" / "raw" / args.name
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "matrix_plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        print(f"resuming saved plan ({len(plan['order'])} trials)")
    else:
        cells = [list(c) for c in itertools.product(args.strategies, args.scenarios, range(1, args.reps + 1))]
        random.Random(args.seed).shuffle(cells)
        plan = {"seed": args.seed, "created": datetime.now().isoformat(), "order": cells, "passthrough": passthrough}
        plan_path.write_text(json.dumps(plan, indent=2))

    if args.dry_list:
        for i, (s, c, r) in enumerate(plan["order"], 1):
            print(f"{i:3d}  {s:10s} {c:9s} r{r}")
        return 0

    failures = 0
    for i, (strategy, scenario, rep) in enumerate(plan["order"], 1):
        tid = f"{args.name}_{strategy}_{scenario}_r{rep}"
        tdir = out / tid
        if (tdir / "metadata.json").exists():
            meta = json.loads((tdir / "metadata.json").read_text())
            if meta.get("completeness", {}).get("ok"):
                print(f"[{i}/{len(plan['order'])}] skip {tid} (complete)")
                continue
        if tdir.exists():
            dest = out / "_incomplete" / f"{tid}_{datetime.now():%H%M%S}"
            dest.parent.mkdir(exist_ok=True)
            shutil.move(str(tdir), dest)
            print(f"moved incomplete {tid} -> {dest.relative_to(REPO_ROOT)}")
        print(f"[{i}/{len(plan['order'])}] run {tid}")
        rc = subprocess.call([sys.executable, str(RUN_TRIAL), "--strategy", strategy, "--scenario", scenario,
                              "--rep", str(rep), "--trial-id", tid, "--results-dir", str(out), *passthrough])
        if rc != 0:
            failures += 1
            print(f"  -> {tid} incomplete (rc={rc}); rerun this script to retry")
        time.sleep(args.gap_s)
    print(f"done; {failures} incomplete trial(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
