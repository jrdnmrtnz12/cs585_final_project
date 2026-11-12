"""Summarize raw trials into per-trial and per-cell tables.

    python analysis/summarize.py                          # all trials under results/raw
    python analysis/summarize.py --results-dir results/raw/final1 --label final
    python analysis/summarize.py --accuracy results/accuracy_by_profile.csv

Definitions (frames captured during the warm-up are excluded):
  latency_*         over frames with outcome=ok, from latency_ms (capture -> response, client clock)
  pct_meet_target   ok frames with latency <= target / ALL frames sent (timeouts and errors count as misses)
  stale             ok frames with latency > stale_ms (stale_factor * target)
  eff_fps           ok responses per second
  uplink_payload_mbps  JPEG bytes sent * 8 / time. APPLICATION PAYLOAD ONLY (no HTTP/TCP/IP/802.11 headers)
  accuracy_est      sum over profiles of (share of ok frames at profile p) * F1(p), where F1 comes from
                    analysis/accuracy_offline.py: agreement with a full-res reference, NOT ground truth

Cells (strategy x scenario) are summarized ACROSS TRIALS: the trial is the unit
of replication, never the frame. CI = 95% t-interval over per-trial values.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent

# two-sided 95% t critical values by degrees of freedom (n - 1)
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
       10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086, 30: 2.042}


def t95(df: int) -> float:
    if df <= 0:
        return float("nan")
    keys = [k for k in sorted(T95) if k <= df]
    return T95[keys[-1]] if df <= 30 else 1.96


def summarize_trial(tdir: Path, accuracy: dict[str, float] | None) -> dict[str, object] | None:
    meta_path = tdir / "metadata.json"
    if not (meta_path.exists() and (tdir / "frames.csv").exists()):
        return None
    meta = json.loads(meta_path.read_text())
    frames = pd.read_csv(tdir / "frames.csv")
    warm_ns = meta["warmup_s"] * 1e9
    f = frames[frames["capture_ns"] >= warm_ns]
    ok = f[f["outcome"] == "ok"]
    dur = max(meta["duration_s"] - meta["warmup_s"], 1e-9)
    target, stale = meta["target_ms"], meta["stale_ms"]
    lat = ok["latency_ms"]
    events = pd.read_csv(tdir / "events.csv") if (tdir / "events.csv").exists() else pd.DataFrame()
    superseded = int(((events["event"] == "superseded") & (events["t_ns"] >= warm_ns)).sum()) if len(events) else 0
    switches = pd.read_csv(tdir / "switches.csv") if (tdir / "switches.csv").exists() else pd.DataFrame()
    share = ok["profile"].value_counts(normalize=True).to_dict() if len(ok) else {}
    row: dict[str, object] = {
        "trial_id": meta["trial_id"], "label": meta.get("label"), "strategy": meta["strategy"],
        "scenario": meta["scenario"], "rep": meta["rep"], "complete": meta["completeness"]["ok"],
        "mock": meta["server"]["health"]["mock"], "source": meta["source"]["kind"], "target_ms": target,
        "sent": len(f), "ok": len(ok), "timeouts": int((f["outcome"] == "timeout").sum()),
        "errors": int(f["outcome"].isin(["http_error", "conn_error"]).sum()), "superseded": superseded,
        "latency_p50_ms": lat.median() if len(lat) else math.nan,
        "latency_p95_ms": lat.quantile(0.95) if len(lat) else math.nan,
        "latency_max_ms": lat.max() if len(lat) else math.nan,
        "latency_std_ms": lat.std() if len(lat) > 1 else math.nan,
        "pct_meet_target": 100.0 * (lat <= target).sum() / len(f) if len(f) else math.nan,
        "stale": int((lat > stale).sum()),
        "eff_fps": len(ok) / dur,
        "uplink_payload_mbps": f["jpeg_bytes"].sum() * 8 / dur / 1e6,
        "encode_ms_mean": f["encode_ms"].mean(),
        "server_ms_mean": ok["server_total_ms"].mean() if len(ok) else math.nan,
        "residual_ms_mean": (ok["latency_ms"] - ok["encode_ms"] - ok["server_total_ms"]).mean() if len(ok) else math.nan,
        "switches": len(switches),
        "server_cpu_pct_mean": (meta.get("server_resources") or {}).get("cpu_pct_mean"),
    }
    for p, s in share.items():
        row[f"share_{p}"] = s
    if accuracy and share:
        row["accuracy_est"] = sum(s * accuracy.get(p, math.nan) for p, s in share.items())
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-dir", default=str(REPO_ROOT / "results" / "raw"))
    ap.add_argument("--out", default=str(REPO_ROOT / "results" / "processed"))
    ap.add_argument("--label", choices=["dev", "pilot", "final"], help="only include trials with this label")
    ap.add_argument("--accuracy", help="CSV from analysis/accuracy_offline.py (columns: profile, f1)")
    ap.add_argument("--include-incomplete", action="store_true")
    args = ap.parse_args(argv)

    accuracy = None
    if args.accuracy:
        acc = pd.read_csv(args.accuracy)
        accuracy = dict(zip(acc["profile"], acc["f1"]))

    rows = []
    for meta_path in sorted(Path(args.results_dir).rglob("metadata.json")):
        if "_incomplete" in meta_path.parts:
            continue
        r = summarize_trial(meta_path.parent, accuracy)
        if r and (args.include_incomplete or r["complete"]) and (not args.label or r["label"] == args.label):
            rows.append(r)
    if not rows:
        print("no trials found")
        return 1
    trials = pd.DataFrame(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    trials.to_csv(out / "trials.csv", index=False)

    metrics = [m for m in ("latency_p50_ms", "latency_p95_ms", "pct_meet_target", "eff_fps",
                           "uplink_payload_mbps", "accuracy_est", "switches") if m in trials]
    cells = []
    for (strategy, scenario), g in trials.groupby(["strategy", "scenario"]):
        c: dict[str, object] = {"strategy": strategy, "scenario": scenario, "n_trials": len(g)}
        for m in metrics:
            vals = g[m].dropna()
            c[f"{m}_mean"] = vals.mean() if len(vals) else math.nan
            c[f"{m}_ci95"] = t95(len(vals) - 1) * vals.std(ddof=1) / math.sqrt(len(vals)) if len(vals) > 1 else math.nan
        cells.append(c)
    cells_df = pd.DataFrame(cells)
    cells_df.to_csv(out / "cells.csv", index=False)

    if trials["mock"].any():
        print("WARNING: includes MOCK-detector trials: pipeline testing only, not results")
    show = ["strategy", "scenario", "n_trials"] + [f"{m}_mean" for m in metrics[:5]]
    with pd.option_context("display.width", 160, "display.max_columns", 20, "display.float_format", "{:.1f}".format):
        print(cells_df[show].to_string(index=False))
    print(f"\nwrote {out / 'trials.csv'} and {out / 'cells.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
