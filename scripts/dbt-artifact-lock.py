#!/usr/bin/env python3
"""Hold the repo-wide dbt artifact publication lock until the owner shell exits."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import sys
import threading
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    args = parser.parse_args()

    args.lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another dbt artifact publisher is active", file=sys.stderr)
            return 75

        # The file can remain after a crash; the kernel lock cannot. Recording
        # the holder is diagnostic only and never used as an unsafe stale-PID
        # deletion signal.
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"owner_pid={args.owner_pid}\n".encode("ascii"))
        os.fsync(descriptor)
        args.ready.parent.mkdir(parents=True, exist_ok=True)
        args.ready.write_text("ready\n", encoding="utf-8")

        stop = threading.Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        while not stop.wait(0.2):
            # A SIGKILLed shell cannot run its trap. The holder becomes an
            # orphan, detects re-parenting, and releases the advisory lock.
            if os.getppid() != args.owner_pid:
                break
        return 0
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
