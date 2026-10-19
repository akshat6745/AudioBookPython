#!/usr/bin/env python3
"""Follow the launched student-TTS app until a GPU stage is actually working, or a stage fails."""
from __future__ import annotations

import select
import subprocess
import sys
import time
from pathlib import Path

APP_ID_FILE = Path("student_tts_app_id.txt")  # written by queue_student_tts.py
TIMEOUT_S = 45 * 60
# Lines only printed from inside a running GPU function. "STAGE start <x>" is not enough: the
# orchestrator prints it before the GPU container exists.
GPU_MARKERS = ("GPU_TELEMETRY ", "HIGGS_THROUGHPUT ", "healthy at concurrency", "CALIBRATION ", "CUDA_SETTINGS ")
FAILURE_MARKERS = ("Traceback (most recent call last)", "ModuleNotFoundError", "Runner failed")


def observe(line: str) -> str | None:
    """'gpu' once a GPU stage is working, 'failed' on a stage error, else None."""
    if any(m in line for m in FAILURE_MARKERS):
        return "failed"
    if any(m in line for m in GPU_MARKERS):
        return "gpu"
    return None


def main() -> int:
    app_id = APP_ID_FILE.read_text().strip()
    print(f"following {app_id}", flush=True)
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        proc = subprocess.Popen(
            ["modal", "app", "logs", app_id, "--timestamps", "--follow"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 10)
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                print(line, end="", flush=True)
                state = observe(line)
                if state:
                    proc.terminate()
                    if state == "gpu":
                        print("verified student-TTS GPU stage activity")
                        return 0
                    print("student-TTS stage failed; see the traceback above", file=sys.stderr)
                    return 1
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
