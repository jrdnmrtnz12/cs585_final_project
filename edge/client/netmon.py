"""Network measurements available to a real client.

UdpProber   - sends small UDP probes to the server's echo port and reports, per
              window, the base RTT (minimum), the median RTT, and the loss rate.
              Because the probes share the uplink queue with the JPEG uploads,
              median - min RTT shows queueing delay (Kurose & Ross Ch. 1).
              Loss is ROUND-TRIP probe loss: with uplink-only impairment that is
              approximately the uplink loss rate.

GoodputEstimator - passive estimate of upload goodput from the frames we
              actually send:  goodput = 8 * bytes / (request_rtt - server_time - base_rtt).
              This is the rate our uploads ACHIEVE, including TCP slow start and
              retransmissions. It is NOT the link capacity. If transfers are
              too short to time reliably, the link is not the bottleneck at that
              frame size, and the estimate is reported as unknown (None).
"""
from __future__ import annotations

import socket
import statistics
import threading
import time
from collections import deque
from typing import Callable

from edge import protocol as P

# Base RTT = minimum probe RTT over this many seconds. Short enough to follow an
# increase in added delay within a few windows, long enough to find a probe
# that did not wait behind a frame upload.
BASE_RTT_HORIZON_S = 3.0


class UdpProber:
    def __init__(self, host: str, port: int, probe_hz: float = 20.0, timeout_s: float = 1.0,
                 clock_ns: Callable[[], int] = time.monotonic_ns):
        self.addr = (host, port)
        self.period_s = 1.0 / probe_hz
        self.timeout_ns = int(timeout_s * 1e9)
        self.clock_ns = clock_ns
        self._lock = threading.Lock()
        self._sent: dict[int, int] = {}                     # seq -> send_ns
        self._replied: set[int] = set()
        self._replies: deque[tuple[int, float]] = deque()   # (recv_ns, rtt_ms)
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._base_rtt: deque[tuple[int, float]] = deque()  # (t_ns, window min) for a rolling base RTT

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.2)
        self._sock.connect(self.addr)
        self._threads = [threading.Thread(target=self._send_loop, name="probe-tx", daemon=True),
                         threading.Thread(target=self._recv_loop, name="probe-rx", daemon=True)]
        for th in self._threads:
            th.start()

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=2)
        if self._sock:
            self._sock.close()

    def _send_loop(self) -> None:
        seq = 0
        next_t = time.monotonic()
        while not self._stop.is_set():
            seq += 1
            now = self.clock_ns()
            with self._lock:
                self._sent[seq] = now
            try:
                self._sock.send(P.pack_probe(seq, now))  # type: ignore[union-attr]
            except OSError:
                pass  # counted as lost: it is in _sent but will never be replied to
            next_t += self.period_s
            delay = next_t - time.monotonic()
            if delay < -self.period_s:
                next_t = time.monotonic()
            elif delay > 0:
                self._stop.wait(delay)

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._sock.recv(2048)  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:  # e.g. ICMP port unreachable before the server is up
                time.sleep(0.05)
                continue
            now = self.clock_ns()
            try:
                seq, send_ns = P.unpack_probe(data)
            except Exception:
                continue
            with self._lock:
                if seq in self._sent and seq not in self._replied:
                    self._replied.add(seq)
                    self._replies.append((now, (now - send_ns) / 1e6))

    def window(self, t_end_ns: int, window_ns: int) -> dict[str, float | int | None]:
        """Stats for the window ending at t_end_ns.

        RTT comes from echoes received in [t_end - window, t_end). Loss comes from
        probes SENT in [t_end - window - timeout, t_end - timeout), which have
        all had a full timeout to come back, so the loss figure lags by one timeout.
        """
        lo_rtt = t_end_ns - window_ns
        lo_loss, hi_loss = t_end_ns - window_ns - self.timeout_ns, t_end_ns - self.timeout_ns
        with self._lock:
            rtts = [r for (t, r) in self._replies if lo_rtt <= t < t_end_ns]
            due = [q for q, ts in self._sent.items() if lo_loss <= ts < hi_loss]
            lost = sum(1 for q in due if q not in self._replied)
            # prune state older than the loss horizon
            while self._replies and self._replies[0][0] < lo_loss:
                self._replies.popleft()
            for q in [q for q, ts in self._sent.items() if ts < lo_loss]:
                self._sent.pop(q, None)
                self._replied.discard(q)
        wmin = min(rtts) if rtts else None
        if wmin is not None:
            self._base_rtt.append((t_end_ns, wmin))
        while self._base_rtt and self._base_rtt[0][0] <= t_end_ns - int(BASE_RTT_HORIZON_S * 1e9):
            self._base_rtt.popleft()
        base = min((r for _, r in self._base_rtt), default=None)
        return {
            "rtt_ms_min": wmin,
            "rtt_ms_base": base,
            "rtt_ms_median": statistics.median(rtts) if rtts else None,
            "probes_sent": len(due),
            "probes_lost": lost,
            "loss_pct": (100.0 * lost / len(due)) if due else None,
        }


class GoodputEstimator:
    def __init__(self, alpha: float = 0.3, min_transfer_ms: float = 5.0):
        self.alpha = alpha
        self.min_transfer_ms = min_transfer_ms
        self.value: float | None = None
        self._lock = threading.Lock()
        self._informative = 0
        self._too_fast = 0

    def add(self, nbytes: int, request_rtt_ms: float, server_ms: float, base_rtt_ms: float | None) -> float | None:
        """Add one completed upload; returns the sample in Mbit/s, or None if not informative."""
        if base_rtt_ms is None:
            return None
        transfer_ms = request_rtt_ms - server_ms - base_rtt_ms
        with self._lock:
            if transfer_ms < self.min_transfer_ms:
                self._too_fast += 1
                return None
            sample = nbytes * 8 / (transfer_ms / 1e3) / 1e6
            self.value = sample if self.value is None else (1 - self.alpha) * self.value + self.alpha * sample
            self._informative += 1
            return sample

    def close_window(self) -> float | None:
        """Call once per window. If every upload was too fast to time, goodput is unknown."""
        with self._lock:
            if self._informative == 0 and self._too_fast > 0:
                self.value = None
            self._informative = self._too_fast = 0
            return self.value
