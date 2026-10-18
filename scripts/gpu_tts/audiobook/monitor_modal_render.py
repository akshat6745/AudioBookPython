#!/usr/bin/env python3
"""Verify that a detached Modal render reaches real chapter generation."""
from __future__ import annotations

import select
import subprocess
import sys
import time

APP = "audiobook-render"
TIMEOUT_S = 45 * 60


def observe(line: str, healthy: bool) -> tuple[bool, bool]:
    healthy = healthy or "healthy at concurrency" in line
    rendered = healthy and "[phase 2]" in line and ": rendered " in line
    return healthy, rendered


def main() -> int:
    deadline = time.monotonic() + TIMEOUT_S
    healthy = False
    while time.monotonic() < deadline:
        proc = subprocess.Popen(
            [
                "modal",
                "app",
                "logs",
                APP,
                "--follow",
                "--since",
                "3m",
                "--timestamps",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 10)
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                print(line, end="", flush=True)
                healthy, rendered = observe(line, healthy)
                if rendered:
                    proc.terminate()
                    print("verified healthy server and actual chapter render")
                    return 0
            elif proc.poll() is not None:
                break
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(10)
    print("timed out before a healthy server rendered a chapter", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
