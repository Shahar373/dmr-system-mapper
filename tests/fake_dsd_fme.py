#!/usr/bin/env python3
"""Stand-in for the dsd-fme binary in offline tests.

Accepts the real command line (``-fs -i tcp:host:port -o null -Z -D 0 [-xr]``), connects to the
PCM bridge like DSD-FME does, drains whatever it is sent, and prints DSD-FME-formatted lines on
stderr: by default the first lines of the Cap+ sample, or ``-DMR`` lines when FAKE_DSD_INVERTED
is set so the polarity retry can be tested. Exits when the bridge closes or on SIGTERM.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "capplus" / "dsd_fme_168863_20260509_163344.log"


def main(argv: list[str]) -> int:
    host, port = "127.0.0.1", 7355
    inverted = "-xr" in argv
    for i, a in enumerate(argv):
        if a == "-i" and i + 1 < len(argv) and argv[i + 1].startswith("tcp"):
            parts = argv[i + 1].split(":")
            if len(parts) == 3:
                host, port = parts[1], int(parts[2])
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    sock = socket.create_connection((host, port), timeout=5)
    received = [0]

    def drain() -> None:
        try:
            while not stop.is_set():
                data = sock.recv(65536)
                if not data:
                    break
                received[0] += len(data)
        except OSError:
            pass
        stop.set()

    threading.Thread(target=drain, daemon=True).start()
    print("Build Version: fake dsd-fme for tests", file=sys.stderr, flush=True)
    rate = float(os.environ.get("FAKE_DSD_LINES_PER_S", "3000"))
    emit_inverted = os.environ.get("FAKE_DSD_INVERTED") == "1" and not inverted
    with SAMPLE.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    i = 0
    t0 = time.monotonic()
    while not stop.is_set():
        line = lines[i % len(lines)]
        if emit_inverted:
            line = line.replace("Sync: +DMR", "Sync: -DMR")
        sys.stderr.write(line)
        i += 1
        if i % 50 == 0:
            sys.stderr.flush()
            expected = i / rate
            lag = expected - (time.monotonic() - t0)
            if lag > 0:
                time.sleep(min(lag, 0.05))
    sys.stderr.flush()
    print(f"Exiting. received {received[0]} bytes", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
