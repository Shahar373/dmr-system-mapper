"""PCM bridge: a tiny TCP server per DSD-FME instance. DSD-FME connects as a client
(``-i tcp:127.0.0.1:<port>``) and reads 48 kHz 16-bit mono PCM, the same format SDR++ and GQRX
serve. We are the server so DSD-FME can be started after the port is open and restarted freely.

With ``backlog_bytes`` the server keeps what is written before the client connects and sends it
first: a slice watcher writes a channel's pre-roll (the moments before its key-up) while the
DSD-FME it just started is still connecting.
"""

from __future__ import annotations

import socket
import threading
import time

import numpy as np


class PcmServer:
    def __init__(self, port: int, host: str = "127.0.0.1", send_timeout_s: float = 0.5, backlog_bytes: int = 0):
        self.port = port
        self.host = host
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(1)
        self._srv.settimeout(0.5)
        self._client: socket.socket | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._timeout = send_timeout_s
        self.bytes_sent = 0
        self.drops = 0
        self._backlog: list[bytes] = []
        self._backlog_max = int(backlog_bytes)
        self._backlog_len = 0
        self._thread = threading.Thread(target=self._accept_loop, name=f"pcm-accept-{port}", daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._client is not None

    def wait_client(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._client is not None:
                return True
            time.sleep(0.05)
        return self._client is not None

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(self._timeout)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                old, self._client = self._client, conn
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass

    def write(self, pcm: np.ndarray) -> bool:
        """Send int16 samples. Returns False when nothing is connected or the client fell behind
        (the block is dropped rather than letting the radio pipeline stall)."""
        data = np.ascontiguousarray(pcm, dtype=np.int16).tobytes()
        with self._lock:
            c = self._client
            if c is None:
                if self._backlog_max:
                    self._backlog.append(data)
                    self._backlog_len += len(data)
                    while self._backlog_len > self._backlog_max and len(self._backlog) > 1:
                        self._backlog_len -= len(self._backlog.pop(0))
                return False
            try:
                if self._backlog:
                    data = b"".join(self._backlog) + data
                    self._backlog, self._backlog_len = [], 0
                c.sendall(data)
                self.bytes_sent += len(data)
                return True
            except (socket.timeout, OSError):
                self.drops += 1
                try:
                    c.close()
                except OSError:
                    pass
                self._client = None
                return False

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            if self._client is not None:
                try:
                    self._client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._client.close()
                self._client = None
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=1)
