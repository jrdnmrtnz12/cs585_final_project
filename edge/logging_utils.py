"""Trial directories and fixed-schema CSV logs.

All *_ns columns are CLIENT monotonic-clock nanoseconds RELATIVE TO TRIAL
START. All *_ms columns are durations. Server timings are durations measured
on the server's own clock and returned in each response, so no cross-machine
clock synchronization is needed.
"""
from __future__ import annotations

import csv
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

FRAMES_COLUMNS = (
    "trial_id", "frame_id", "profile",
    "capture_ns", "send_ns", "recv_ns",
    "encode_ms", "jpeg_bytes", "width_px", "height_px", "jpeg_quality",
    "http_status", "server_decode_ms", "server_infer_ms", "server_total_ms", "n_detections",
    "latency_ms",       # recv - capture: what the wearer experiences
    "request_rtt_ms",   # recv - send: one HTTP request/response
    "outcome",          # ok | timeout | http_error | conn_error
)

NETMON_COLUMNS = (
    "trial_id", "t_ns", "profile",
    "rtt_ms_min", "rtt_ms_base", "rtt_ms_median", "loss_pct", "probes_sent", "probes_lost",
    "goodput_mbps_est", "latency_ms_median", "n_responses", "server_ms_median", "oldest_inflight_ms",
)

SWITCHES_COLUMNS = (
    "trial_id", "t_ns", "from_profile", "to_profile", "reason",
    "rtt_ms", "loss_pct", "goodput_mbps", "latency_ms_median", "predicted_ms",
)

EVENTS_COLUMNS = ("trial_id", "t_ns", "event", "frame_id", "detail")

RESOURCES_COLUMNS = ("trial_id", "t_ns", "proc_cpu_pct", "sys_cpu_pct", "proc_rss_mb")


class CsvLog:
    """Append-only CSV with a fixed header. Thread-safe; flushes every row."""

    def __init__(self, path: Path, columns: Iterable[str], trial_id: str):
        self.path = Path(path)
        self.columns = tuple(columns)
        self.trial_id = trial_id
        self._lock = threading.Lock()
        self._file = open(self.path, "x", newline="")  # "x": never overwrite an existing log
        self._writer = csv.DictWriter(self._file, fieldnames=self.columns)
        self._writer.writeheader()
        self._file.flush()
        self.rows = 0

    def write(self, **row: Any) -> None:
        unknown = set(row) - set(self.columns)
        if unknown:
            raise KeyError(f"{self.path.name}: unknown column(s) {sorted(unknown)}")
        row.setdefault("trial_id", self.trial_id)
        out = {c: _fmt(row.get(c)) for c in self.columns}
        with self._lock:
            self._writer.writerow(out)
            self._file.flush()
            self.rows += 1

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()


def _fmt(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3f}"
    return v


def make_trial_id(strategy: str, scenario: str, rep: int, prefix: str | None = None) -> str:
    prefix = prefix or datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}_{strategy}_{scenario}_r{rep}"


def new_trial_dir(results_dir: Path | str, trial_id: str) -> Path:
    """Create results_dir/trial_id. Fails if it already exists, so raw data is never overwritten."""
    path = Path(results_dir) / trial_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)
