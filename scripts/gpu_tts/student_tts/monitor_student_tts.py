#!/usr/bin/env python3
"""Watch student-tts-eye-v1 until GPU work starts or the job fails."""
from __future__ import annotations

import select
import subprocess
import sys
import time

APP = "student-tts-eye-v1"
TIMEOUT_S = 45 * 60


def observe(line: str, selection: bool, gpu: bool) -> tuple[bool, bool]:
    selection = selection or "STAGE start selection" in line
    gpu = gpu or (
        "STAGE start generation" in line
        or "healthy at concurrency" in line
        or "STAGE start training" in line
        or "CALIBRATION " in line
        or "CUDA_SETTINGS " in line
    )
    return selection, gpu


def main() -> int:
    deadline = time.monotonic() + TIMEOUT_S
    selection = gpu = False
    while time.monotonic() < deadline:
        proc = subprocess.Popen(
            ["modal", "app", "logs", APP, "--follow", "--since", "3m", "--timestamps"],
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
                selection, gpu = observe(line, selection, gpu)
                if gpu:
                    proc.terminate()
                    print("verified student-TTS GPU stage activity")
                    return 0
            elif proc.poll() is not None:
                break
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(10)
    print("timed out before student-TTS GPU activity", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
