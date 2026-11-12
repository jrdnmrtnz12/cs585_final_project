#!/usr/bin/env bash
# Verify that a scenario actually produces the intended conditions (run on the client).
#
#   NETEM_IF=wlan0 SERVER_IP=10.58.5.10 netem/verify.sh poor
#
# Applies the scenario, then measures:
#   - RTT and loss with 200 ICMP pings (0.05 s apart, needs root for <0.2 s intervals)
#   - uplink throughput with iperf3 client->server for 15 s (run `iperf3 -s` on the server)
# then resets the interface. Output goes to results/calibration/<timestamp>_<scenario>/.
# Compare against config/scenarios.yaml. Never assume tc did what we asked.
set -euo pipefail

SCENARIO="${1:?usage: verify.sh <scenario>}"
: "${NETEM_IF:?set NETEM_IF}"; : "${SERVER_IP:?set SERVER_IP}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/results/calibration/$(date +%Y%m%d-%H%M%S)_${SCENARIO}"
mkdir -p "$OUT"

cleanup() { "$REPO_ROOT/netem/netem.sh" reset >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

"$REPO_ROOT/netem/netem.sh" apply "$SCENARIO" | tee "$OUT/tc_commands.txt"
"$REPO_ROOT/netem/netem.sh" show > "$OUT/tc_show_before.txt"

echo "== ping (200 probes) =="
sudo ping -c 200 -i 0.05 "$SERVER_IP" | tee "$OUT/ping.txt" | tail -3

echo "== iperf3 uplink (15 s) =="
iperf3 -c "$SERVER_IP" -t 15 -J > "$OUT/iperf3_uplink.json" || echo "iperf3 failed (is 'iperf3 -s' running on the server?)"
python3 - "$OUT/iperf3_uplink.json" <<'EOF' || true
import json, sys
d = json.load(open(sys.argv[1]))
s = d["end"]["sum_sent"]
print(f"uplink {s['bits_per_second']/1e6:.2f} Mbit/s, retransmits {s.get('retransmits', 'n/a')}")
EOF

"$REPO_ROOT/netem/netem.sh" show > "$OUT/tc_show_after.txt"
uname -a > "$OUT/host.txt"; sysctl net.ipv4.tcp_slow_start_after_idle net.ipv4.tcp_congestion_control >> "$OUT/host.txt" 2>/dev/null || true
echo "saved to $OUT"
