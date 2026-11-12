#!/usr/bin/env bash
# Apply / change / reset uplink impairment on the CLIENT (Linux only).
#
#   NETEM_IF=wlan0 SERVER_IP=10.58.5.10 netem/netem.sh apply  poor  [--dry-run]
#   NETEM_IF=wlan0 SERVER_IP=10.58.5.10 netem/netem.sh change good  [--dry-run]
#   NETEM_IF=wlan0                      netem/netem.sh reset        [--dry-run]
#   NETEM_IF=wlan0                      netem/netem.sh show
#
# What it builds on $NETEM_IF egress (client -> network):
#
#   root prio qdisc (handle 1:), priomap sends ALL traffic to band 1:2 (unimpaired)
#     band 1:3 -> netem (handle 30:) with delay / loss / rate
#   u32 filter: packets with destination $SERVER_IP -> band 1:3
#
# So ONLY traffic to the edge server is impaired (HTTP frames AND UDP probes).
# SSH and everything else on the interface is untouched. The delay applies to
# one direction only (uplink), so it adds exactly that much to the RTT.
#
# Every scenario always sets delay, loss AND rate explicitly ("uncapped" =
# 1000mbit), so `tc qdisc change` never inherits an option from the previous
# scenario. Scenario values come from config/scenarios.yaml via
# `python -m edge.config`.
#
# Requires: iproute2 (tc), root (the script uses sudo), the sch_netem and
# sch_prio kernel modules. ALWAYS verify the result with netem/verify.sh.
set -euo pipefail

usage() { sed -n '2,8p' "$0"; exit 2; }

ACTION="${1:-}"; [[ -z "$ACTION" ]] && usage
SCENARIO=""
DRY_RUN=0
shift
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -*) echo "unknown flag $arg" >&2; usage ;;
    *) SCENARIO="$arg" ;;
  esac
done

: "${NETEM_IF:?set NETEM_IF to the client interface carrying experiment traffic (e.g. wlan0)}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
[[ -x "$REPO_ROOT/.venv/bin/python" ]] && PYTHON="$REPO_ROOT/.venv/bin/python"

if [[ $DRY_RUN -eq 0 && "$(uname -s)" != "Linux" ]]; then
  echo "tc/netem only exists on Linux. Use --dry-run to preview the commands." >&2
  exit 1
fi

SUDO="sudo"; [[ $EUID -eq 0 ]] && SUDO=""
run() {
  echo "+ $*"
  if [[ $DRY_RUN -eq 0 ]]; then $SUDO "$@"; fi
}
# Remove any existing root qdisc; "no qdisc" is not an error.
clear_root() {
  echo "+ tc qdisc del dev $NETEM_IF root   (errors ignored)"
  if [[ $DRY_RUN -eq 0 ]]; then $SUDO tc qdisc del dev "$NETEM_IF" root 2>/dev/null || true; fi
}

netem_args() {
  local d l r
  read -r d l r < <(cd "$REPO_ROOT" && "$PYTHON" -m edge.config --scenario "$1")
  echo "delay ${d}ms loss ${l}% rate ${r}mbit limit 1000"
}

warn_if_remote() {
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    local peer="${SSH_CONNECTION%% *}"
    echo "note: running over SSH from $peer; only traffic to ${SERVER_IP:-?} is impaired." >&2
    if [[ "$peer" == "${SERVER_IP:-}" ]]; then
      echo "WARNING: your SSH session comes from SERVER_IP and WILL be impaired." >&2
    fi
  fi
}

case "$ACTION" in
  apply)
    [[ -n "$SCENARIO" ]] || usage
    : "${SERVER_IP:?set SERVER_IP to the edge server address}"
    warn_if_remote
    ARGS="$(netem_args "$SCENARIO")"
    clear_root   # start from a clean interface
    run tc qdisc add dev "$NETEM_IF" root handle 1: prio bands 3 priomap 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1
    # shellcheck disable=SC2086
    run tc qdisc add dev "$NETEM_IF" parent 1:3 handle 30: netem $ARGS
    run tc filter add dev "$NETEM_IF" parent 1: protocol ip prio 1 u32 match ip dst "$SERVER_IP/32" flowid 1:3
    ;;
  change)
    [[ -n "$SCENARIO" ]] || usage
    ARGS="$(netem_args "$SCENARIO")"
    # shellcheck disable=SC2086
    run tc qdisc change dev "$NETEM_IF" parent 1:3 handle 30: netem $ARGS
    ;;
  reset)
    clear_root
    ;;
  show)
    tc -s qdisc show dev "$NETEM_IF"
    tc filter show dev "$NETEM_IF"
    ;;
  *) usage ;;
esac
