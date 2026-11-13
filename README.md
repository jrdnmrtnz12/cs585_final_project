# Adaptive edge offloading under changing network conditions (CS 585, Team 7)

A smart-glasses-style client captures a repeatable video from its screen, JPEG-encodes frames, and sends them to an
edge server running YOLO object detection. We compare **Fixed High**, **Fixed Low**, and an **Adaptive** strategy
that changes resolution, JPEG quality, and frame rate based on measured RTT, upload goodput, and packet loss, under
emulated Good / Moderate / Poor / Dynamic network conditions.

**Read [docs/spec.md](docs/spec.md) first.** It covers scope, design, and the schedule.
Testbed details go in [docs/testbed.md](docs/testbed.md).

## Status (Nov 12)

| Part | State |
|---|---|
| Edge server (`/health`, `/infer`), mock + YOLO11n detector, UDP echo | working, tested |
| Client: screen/video/synthetic capture, 16:9 encoder, latest-frame sender | Windows screen capture tested over Opal Wi-Fi against Mac YOLO11n: 20 s, 271/271 sent frames answered, zero errors (dev run) |
| Network monitor (UDP RTT/loss probes, passive goodput) | working on loopback; needs real-network check |
| Adaptive controller (hysteresis, cooldown, failed-probe backoff) | implemented + unit tested; thresholds provisional |
| `netem.sh` / `verify.sh` | dry-run checked only; **needs validation on the Linux client** |
| Trial runner, matrix runner, summarizer, offline accuracy | working on smoke data |
| `analysis/plots.py` | TODO |
| Router config, real trials, calibration | Opal configured; controlled trials and calibration pending |

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

Run the YOLO server on any machine with `requirements-server.txt` installed and
an IP address reachable from the client. The Opal is the network between them;
it does not run YOLO. Use that server's IP in the client commands below.

```bash
# server (macOS or Linux; device auto-selects CPU, CUDA, or MPS)
.venv/bin/python -m edge.server --detector yolo

# Linux client (Xorg session); replace these with the server IP and Wi-Fi interface
export EDGE_SERVER=10.58.5.10 NETEM_IF=wlan0
netem/netem.sh apply poor --dry-run          # preview tc commands
netem/verify.sh poor                          # apply, measure with ping + iperf3, reset
.venv/bin/python experiments/run_trial.py --strategy adaptive --scenario dynamic --source screen --label pilot
.venv/bin/python experiments/run_matrix.py --name final1 --reps 3 --source screen --label final
```

`tc/netem` is Linux traffic control. Our `netem/` scripts use it on the Linux
client to add controlled delay, loss, and bandwidth limits to traffic headed to
the server. This is for the later impaired trials; it is not needed to start
the server or repeat the Windows connectivity check below.

### Reproduce the Windows client progress run (no impairment)

Mac Ethernet (`10.58.5.10`) connects to the Opal LAN. Windows joins the Opal's
5 GHz `cs585-testbed` Wi-Fi. The Opal carries traffic; YOLO runs on the Mac.
Both machines need their project `.venv`; the Windows client also needs
`data/test_video.mp4` (the video is not in Git). To create a missing Windows
environment from the project root:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-client.txt -e .
```

On the server, from the repo root, run `.venv/bin/python -m edge.server --detector yolo`.
This Mac's Homebrew Python also needs `export DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib`
before that command; other servers should omit that workaround.

On Windows, open `data\test_video.mp4` in a player and pause it at the start.
From the project root in PowerShell, run the following, then switch to the player,
make it full screen, and start playback during the 8-second delay:

```powershell
Invoke-RestMethod 'http://10.58.5.10:8000/health'  # expect mock: False
Start-Sleep -Seconds 8
& '.\.venv\Scripts\python.exe' experiments/run_trial.py --strategy adaptive --scenario none --source screen --server 10.58.5.10 --duration 20 --label dev
```

The client writes `frames.csv`, `netmon.csv`, and `metadata.json` under
`results\raw\<trial_id>\`. Check `metadata.json` for `result.status: complete`
and `completeness.ok: true`. This is a dev connectivity check, not an impaired trial.

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
client→server runs over loopback. They do **not** cover tc/netem or real Wi-Fi; the Windows Wi-Fi check above was a separate dev run.

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
