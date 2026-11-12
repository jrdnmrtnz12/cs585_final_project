"""UDP echo used by the client's network monitor to measure RTT and packet loss.

Each datagram is sent back unchanged. UDP has no retransmission, so a probe
that never comes back was lost on the way out or on the way back. That is the
key difference from TCP, which hides loss by retransmitting.
"""
from __future__ import annotations

import logging
import socket
import threading

log = logging.getLogger(__name__)


class UdpEcho:
    def __init__(self, host: str = "0.0.0.0", port: int = 9000):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.echoed = 0

    def start(self) -> int:
        """Bind and start echoing in a background thread. Returns the bound port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(0.2)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="udp-echo", daemon=True)
        self._thread.start()
        log.info("UDP echo listening on %s:%d", self.host, self.port)
        return self.port

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._sock.sendto(data, addr)
                self.echoed += 1
            except OSError as e:
                log.warning("UDP echo send failed: %s", e)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()
