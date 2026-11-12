# CS 585 Team 7: Project Spec (revised Nov 11, Week 13)

This is the single source of truth for scope, design, and schedule. Give it to any teammate, or to any
tool you use, as context before working on the repo. If something here conflicts with the approved
proposal, the proposal wins unless the team explicitly agrees to change it.

**Team:** Andres/Victor Andrade (client capture/encoding, literature) · Dmitri Azih (testbed, netem, automation) ·
Jordan Martinez (edge server, detection, adaptive controller) · Ben Miller (integration, instrumentation, analysis)

---

## 1. Research question (unchanged from the proposal)

Can an adaptive edge-offloading strategy for a smart-glasses-style computer-vision app keep response times
more consistent under **changing** wireless conditions than fixed-quality strategies, while keeping
substantially more detection accuracy than a fixed low-quality strategy?

This is a networking experiment. We use a pretrained detector (YOLO11n, COCO) and train nothing.

Strategies: **Fixed High**, **Fixed Low**, **Adaptive** (moves between Low / Medium / High).

## 2. What changed from the original plan, and why

| Change | Why |
|---|---|
| Added a **Dynamic** condition (Good→Poor→Moderate→Good, 20 s each) to Good / Moderate / Poor | The RQ is about *changing* conditions. Under a static condition, adaptive should settle on the best-fitting profile. The dynamic schedule is the one test that actually exercises adaptation |
| Profiles are all **16:9** (1280×720 / 640×360 / 320×180) | The old 640×480 / 320×240 mixed aspect ratios, so frames would be distorted or cropped differently per profile. That is a confound |
| Bandwidth caps are **calibrated from measured profile bitrates** | Otherwise a scenario may not separate the strategies at all |
| Controller thresholds come from the **latency target + delay model** `RTT + 8·L/R + server` | Explainable and tied to Kurose Ch. 1 (d_trans = L/R), instead of arbitrary RTT/bandwidth cutoffs |
| **UDP echo probe** for RTT + loss; **passive goodput** for bandwidth | Cross-platform. Keeps IP-level loss separate from TCP-level effects |
| **Offline per-profile accuracy** (agreement with full-res reference) | Live screen-captured frames have no labels. This is deterministic and reproducible |
| netem on the **Linux client's uplink egress**, filtered to the server IP | JPEGs flow client→server, so the cap must apply to the uplink. One machine controls the impairment and the trial clock. Router firmware netem support is unconfirmed, and flashing is off the table |
| **One router** in the core experiment | Router #2 is only for an optional real-distance demo |
| **No client energy claims**. CPU % and payload bytes as proxies | A USB-C meter on a laptop charger measures charger input, not consumption, because the battery buffers the load (the proposal's own fallback) |
| Latest-frame slot, **max 1 request in flight** | Bounded queue by design. Newest frame is what matters for a wearable |
| Everything logged in **one client clock**; the server returns its own durations | No cross-machine clock sync needed |

## 3. Testbed

```
[Looping test video, fullscreen on client display]
          | screen capture (mss; X11 session on Linux)
[CLIENT: Linux laptop]                                 [EDGE SERVER: Jordan's M5 Mac]
  capture -> encode -> HTTP POST /infer   ---------->   FastAPI :8000  (/health, /infer, /stats)
  UDP probes -> :9000                     ---------->   UDP echo :9000
  tc/netem on Wi-Fi egress, filtered to server IP       YOLO11n on MPS, loaded once
  trial runner + dynamic schedule (same machine)
          |                                                   |
          +-- 5 GHz Wi-Fi --> [GL.iNet Opal #1] <-- Ethernet (USB-C adapter if laptop)
                              router mode, WAN unplugged during trials
                              fixed non-DFS channel + width, WPA2, SSID cs585-testbed
                              LAN 10.58.5.0/24 (avoid GL.iNet default 192.168.8.0/24)
                              DHCP reservations: server .10, client .20
```

- **BrosTrend AX9L** = MediaTek MT7921AU, in-kernel `mt7921u` driver (vendor: kernel ≥ 5.18). Linux only (no macOS
  driver). Opal has no 6 GHz, so use 5 GHz. Use it only if the laptop's built-in Wi-Fi is worse. Check with `lsusb` and
  `modinfo mt7921u`.
- **mss does not work under Wayland.** Log in with an Xorg session on the Linux client.
- Fallbacks: if the server has no Ethernet, put it on Wi-Fi and document the double wireless hop. If no Linux laptop
  exists, use macOS `dnctl`/`pfctl` dummynet on a Mac client and document it as a deviation.
- Record everything in `docs/testbed.md`.

## 4. Software

| Path | Owner | Role |
|---|---|---|
| `edge/protocol.py` | Ben/Jordan | Wire format: headers, JSON response schema, UDP probe format |
| `edge/config.py` + `config/*.yaml` | Jordan | Load and validate profiles, scenarios, experiment settings |
| `edge/logging_utils.py` | Ben | Fixed CSV schemas, never-overwrite trial dirs |
| `edge/server/app.py`, `__main__.py` | Jordan | FastAPI `/health`, `/infer`, `/stats`. Single worker, model loaded once |
| `edge/server/detector.py` | Jordan | `MockDetector` (no boxes, `mock: true`) and `YoloDetector` |
| `edge/server/udp_echo.py` | Jordan | Echo for RTT/loss probes |
| `edge/client/capture.py` | Andres | `ScreenSource` (experiment), `VideoFileSource` + `SyntheticSource` (dev only) |
| `edge/client/encoder.py` | Andres | Center-crop to 16:9 if needed → resize → JPEG |
| `edge/client/sender.py` | Ben | Capture thread, latest-frame slot, persistent HTTP sender |
| `edge/client/netmon.py` | Jordan | UDP prober (base/median RTT, loss) and goodput estimator |
| `edge/client/session.py` | Ben/Jordan | Wires it together. Control loop every 1 s |
| `edge/adaptation/controller.py` | Jordan | `FixedPolicy`, `AdaptiveController` |
| `netem/netem.sh`, `netem/verify.sh` | Dmitri | Apply/change/reset impairment. Verify with ping + iperf3 |
| `experiments/run_trial.py`, `run_matrix.py` | Dmitri/Ben | One trial / randomized resumable matrix |
| `analysis/accuracy_offline.py` | Jordan | Per-profile agreement + bitrate calibration table |
| `analysis/summarize.py` | Ben | Per-trial and per-cell stats with 95% t-intervals |
| `analysis/plots.py` | Ben | **TODO**: figures in section 9 |

### Protocol
`POST /infer`: body = raw JPEG, headers `X-Frame-Id` (required), `X-Trial-Id`, `X-Profile`. The response gives
`frame_id`, `image_wh`, `detections[{class_id,label,conf,bbox_xyxy_px}]`, and
`timing{decode_ms,infer_ms,server_total_ms}`, plus `mock`. Boxes are pixel coordinates in the received image.
`server_total_ms` starts once the body has fully arrived, so it excludes upload time. One persistent HTTP/1.1 connection.

YOLO letterboxes every input to 640 px, so **inference time is about the same for every profile**. The profiles differ
in network cost and in information lost to downscaling and JPEG. State this in the paper.

## 5. Profiles (`config/profiles.yaml`, starting values, calibrate in the pilot)

| Profile | Resolution | JPEG q | FPS |
|---|---|---|---|
| High | 1280×720 | 90 | 15 |
| Medium (adaptive only) | 640×360 | 70 | 10 |
| Low | 320×180 | 45 | 5 |

Fixed strategies never change profile. Adaptive starts at Medium.

## 6. Adaptive controller (`edge/adaptation/controller.py`)

Runs once per 1 s window. It uses only measurable signals and never sees the scenario label.

```
predicted(p) = base_rtt + 8 * size_p / goodput + server_ms
DOWN one level: latency_median > T, or in-flight request older than T,
                or predicted(current) > T, or (use_loss_signal and loss > LOSS_HI)
UP one level:   3 consecutive windows with responses, latency_median < 0.7*T,
                predicted(next) < 0.7*T (or goodput unknown -> "probe"),
                and (use_loss_signal -> loss < LOSS_LO), and upgrade cooldown over
Cooldown 2 s after a change. A downgrade within 3 s of an upgrade = failed probe ->
upgrade cooldown doubles (max 16 s), resets after an upgrade that holds.
No data -> hold.
```
`T` (target, placeholder 300 ms), `LOSS_HI`, and `LOSS_LO` are **provisional until the pilot**. Base T on the
latency bounds in Chen et al. 2017, then check it: High must meet T under Good and miss it under Poor.
`use_loss_signal: false` is the ablation that tests the proposal's claim that RTT + loss beats bandwidth alone.

Every switch is logged (`switches.csv`) with its inputs and reason. This is application-level adaptation of
*how much data we generate*. It is not TCP congestion control, which adapts *how fast the transport sends it*.

## 7. Network impairment

`netem/netem.sh` builds `prio` root → band 1:3 netem, and a u32 filter sends only `dst SERVER_IP` to that band.
- Uplink only, so the configured delay adds that much RTT. SSH and other traffic are untouched.
- Every scenario sets delay, loss, and rate explicitly ("uncapped" = 1000 mbit), so `tc qdisc change` never inherits
  options from the previous scenario.
- `--dry-run` prints the commands. `run_trial.py` always resets in `finally` (Ctrl-C/SIGTERM included).
- Starting values: Good 0/0/uncapped · Moderate 30 ms / 1% / cap · Poor 80 ms / 3% / cap.
- **Calibration rule:** run `analysis/accuracy_offline.py` on the real video and read `bitrate_mbps_at_fps`.
  The Moderate cap goes between the Medium and High bitrates. The Poor cap goes between the Low and Medium bitrates.
- **Verify every scenario** with `netem/verify.sh` (200 pings + 15 s iperf3 uplink). Never assume tc did what we asked.
- One tcpdump pcap per scenario (not per trial) for on-wire overhead and TCP retransmission counts (Wireshark
  `tcp.analysis.retransmission`).

## 8. Experiment

- Matrix: 3 strategies × {good, moderate, poor, dynamic} × 3 reps (5 if time) = 36–60 trials, 60 s each
  (dynamic 80 s), 5 s warm-up excluded. Randomized order (seed 585), resumable: `run_matrix.py`.
- `run_trial.py` refuses `--label pilot/final` unless the server runs real YOLO and `--source screen`.
- Same video, model, display, placement, and router settings for every trial. Deviations go in `metadata.json` notes.

### Logs (`results/raw/<trial_id>/`, never overwritten)
`frames.csv` (one row per sent frame; `latency_ms` = capture→response, `request_rtt_ms` = send→response),
`netmon.csv` (1 Hz: base/min/median RTT, loss, goodput estimate, window latency), `switches.csv`,
`events.csv` (superseded frames, scenario changes, errors), `resources.csv` (client CPU/RSS), `metadata.json`
(config snapshot, versions, git commit, netem commands, server CPU, completeness). All `*_ns` = client monotonic
time since trial start.

## 9. Metrics and analysis

- Latency p50 / p95 / max / sd. **% of frames meeting T** (timeouts count as misses). Stale responses (> 2T).
  Effective FPS. Superseded/timeout counts. **Uplink payload Mbps** (labeled payload, see pcap for overhead).
  CPU %. Accuracy estimate = Σ share(p) × F1(p).
- Accuracy is **agreement with a full-res reference** from the same model, never "accuracy". Stretch goal: true-GT
  check on about 500 COCO val2017 images.
- The unit of replication is the **trial**. Cells = mean of per-trial values ± 95% t-interval (n = 3–5, so wide).
- Figures (`analysis/plots.py`, TODO): (1) p50/p95 latency by condition × strategy; (2) accuracy vs uplink bitrate;
  (3) **adaptive timeline on Dynamic**: profile, RTT, goodput, loss, latency, scenario boundaries; (4) % meeting T;
  (5) time breakdown: encode / server / residual (label it "network + queueing (residual)", not one-way delay).

## 10. Schedule

| When | Work |
|---|---|
| **Wk 13 · Nov 11–15** | Jordan: scaffold, server, controller, local end-to-end ✅. Andres: pick and license video; ScreenSource on Linux/Xorg; measure capture FPS. Dmitri: configure Opal; client Wi-Fi; validate netem.sh with verify.sh. Ben: review schemas; `plots.py` on smoke data |
| **Wk 14 · Nov 16–18** | Integrate on testbed. Per-profile bitrate + accuracy table on the real video. Check netmon against netem (probe loss ≈ configured? goodput tracks cap?) |
| **Nov 19** | Calibrate caps and T. Pilot: 1 rep × 12 cells (~15 min). Fix issues |
| **Nov 20** | **Freeze protocol** (git tag `protocol-v1`). Pilot = preliminary data for any checkpoint |
| **Nov 21–23** | Full matrix (3 reps ≈ 1 h; 5 if clean). pcaps per scenario |
| **Nov 24–25** | Analysis, figures, rerun bad trials, draft slides |
| Nov 26–29 | Thanksgiving buffer |
| **Wk 16 · Nov 30–Dec 4** | Rehearse + present (12–15 min, must *teach something new*). Optional real-distance demo with router #2 |
| After | Final paper (technical / economic / operational feasibility + evaluation + AI reflection). Individual reflection |

Possible "teach us something" angles (hypotheses to test, not findings): does uplink loss → TCP retransmission
dominate tail latency for large frames (so the loss signal matters)? How does reaction time compare with how fast
conditions change? Does constant inference cost make the network the only lever?

## 11. Scope priorities

- **Must:** server + YOLO · capture/encode/send · Fixed High/Low + Adaptive · Good, Poor, Dynamic · 3 reps ·
  latency, bandwidth, agreement.
- **Should:** Moderate · loss signal · 5 reps · pcap retransmission analysis · CPU.
- **Could:** `use_loss_signal` ablation · COCO true-GT check · router #2 demo · router power reading.
- **Won't:** client energy claims · router netem/firmware flashing · Docker · ML/RL controller.

## 12. Rules for working on this repo

- **Never fabricate or "fill in" data.** Label dev / pilot / final. Raw data is immutable. Every number in the
  paper must trace to a trial ID.
- Mock-detector output and dev sources (`video`, `synthetic`) are for pipeline testing only.
- Show and explain any privileged network command before running it. Use `--dry-run` first.
- Make small, tested changes. `pytest` must pass before pushing. Don't claim something works until it has been run.
- Commits are authored by the person who makes them, with no auto-generated attribution trailers.
- The final paper's AI disclosure must name the tools used and at what stage (course policy). Keep that log in
  the team's paper notes.
