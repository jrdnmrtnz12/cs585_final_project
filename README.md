# Adaptive edge offloading under changing network conditions (CS 585, Team 7)

A smart-glasses-style client captures a repeatable video from its screen, JPEG-encodes frames, and sends them to an
edge server running YOLO object detection. We compare **Fixed High**, **Fixed Low**, and an **Adaptive** strategy
that changes resolution, JPEG quality, and frame rate based on measured RTT, upload goodput, and packet loss, under
emulated Good / Moderate / Poor / Dynamic network conditions.

**Read [docs/spec.md](docs/spec.md) first.** It covers scope, design, the schedule, and who owns what.
Testbed details go in [docs/testbed.md](docs/testbed.md).

## Status (Nov 11)

| Part | State |
|---|---|
| Edge server (`/health`, `/infer`), mock + YOLO11n detector, UDP echo | working, tested |
| Client: screen/video/synthetic capture, 16:9 encoder, latest-frame sender | working locally |
| Network monitor (UDP RTT/loss probes, passive goodput) | working on loopback; needs real-network check |
| Adaptive controller (hysteresis, cooldown, failed-probe backoff) | implemented + unit tested; thresholds provisional |
| `netem.sh` / `verify.sh` | dry-run checked only; **needs validation on the Linux client** |
| Trial runner, matrix runner, summarizer, offline accuracy | working on smoke data |
| `analysis/plots.py` | TODO |
| Router config, real trials, calibration | TODO (see spec, section 10) |

## Setup

Python 3.12 recommended (Ubuntu 24.04 ships it; on macOS `brew install python@3.12`).

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-server.txt -e .   # edge server machine (includes YOLO)
.venv/bin/pip install -r requirements-client.txt -e .   # client machine
```

## Quick local run (one machine, no network equipment)

```bash
# terminal 1: server (mock = no model; use --detector yolo for real detection)
.venv/bin/python -m edge.server --detector mock

# terminal 2: a 15 s adaptive trial with synthetic frames, no impairment
.venv/bin/python experiments/run_trial.py --strategy adaptive --scenario none --duration 15 --allow-mock

# summarize everything under results/raw
.venv/bin/python analysis/summarize.py
```

## On the testbed

```bash
# server (M5 Mac)
.venv/bin/python -m edge.server --detector yolo

# client (Linux, Xorg session), after checking each scenario with netem/verify.sh
export EDGE_SERVER=10.58.5.10 NETEM_IF=wlan0
netem/netem.sh apply poor --dry-run          # preview tc commands
netem/verify.sh poor                          # apply, measure with ping + iperf3, reset
.venv/bin/python experiments/run_trial.py --strategy adaptive --scenario dynamic --source screen --label pilot
.venv/bin/python experiments/run_matrix.py --name final1 --reps 3 --source screen --label final
```

## Calibration helpers

```bash
# per-profile JPEG size, bitrate, and agreement with the full-res reference, on the real test video
.venv/bin/python analysis/accuracy_offline.py --video data/test_video.mp4 --every-n 15
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

Tests cover the API (mock detector), encoder, controller logic, logging schemas, UDP probing, and two end-to-end
client→server runs over loopback. They do **not** cover tc/netem or real Wi-Fi. Those need the Linux client.

## Layout

```
config/       profiles.yaml, scenarios.yaml, experiment.yaml (all provisional values flagged)
edge/         protocol, config, logging, server/, client/, adaptation/
netem/        netem.sh (apply/change/reset/show), verify.sh (ping + iperf3 check)
experiments/  run_trial.py, run_matrix.py
analysis/     accuracy_offline.py, summarize.py (plots.py TODO)
docs/         spec.md, testbed.md
data/         README.md only (video/weights not committed)
results/      raw/<trial_id>/ (committed CSV + metadata), processed/, calibration/
```
