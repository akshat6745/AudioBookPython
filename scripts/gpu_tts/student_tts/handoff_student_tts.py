#!/usr/bin/env python3
"""Wait for an older student-TTS app to finish Whisper QA, then stop it before its training stage runs.

  python handoff_student_tts.py <app id>

Exits 0 once the old app is stopped, so the workflow can launch the replacement run, which reuses the
manifest, WAVs, and QA lists already on the Volume.
"""
from __future__ import annotations

import json
import select
import subprocess
import sys
import time

TIMEOUT_S = 150 * 60
# QA has returned (and its Volume commit happened) once the orchestrator moves past it.
QA_FINISHED = ("STAGE done qa", "STAGE start training", "[train ", "CUDA_SETTINGS ", "CALIBRATION")
# Any stage error: Modal would retry it with the old app's code, so stop it and let the new run take over.
QA_FAILED = ("refusing an undersized training set", "QA left only", "Traceback (most recent call last)")


def observe(line: str) -> str | None:
    if any(m in line for m in QA_FINISHED):
        return "qa-finished"
    if any(m in line for m in QA_FAILED):
        return "qa-failed"
    return None


def app_stopped(app_id: str) -> bool:
    try:
        out = subprocess.run(["modal", "app", "list", "--json"], capture_output=True, text=True, timeout=60).stdout
        for app in json.loads(out or "[]"):
            if app_id in json.dumps(app):
                return "stop" in str(app.get("State", app.get("state", ""))).lower()
    except (subprocess.SubprocessError, ValueError):
        pass
    return False


def history_verdict(app_id: str) -> str | None:
    """--follow only streams new lines, so scan recent history for a QA result printed while detached."""
    try:
        out = subprocess.run(["modal", "app", "logs", app_id, "--tail", "2000"],
                             capture_output=True, text=True, timeout=120).stdout
    except subprocess.SubprocessError:
        return None
    verdicts = {observe(line) for line in out.splitlines()} - {None}
    return "qa-finished" if "qa-finished" in verdicts else next(iter(verdicts), None)


def stop(app_id: str) -> None:
    print(f"stopping {app_id}", flush=True)
    code = subprocess.run(["modal", "app", "stop", "--yes", app_id], timeout=120).returncode
    if code and not app_stopped(app_id):
        raise SystemExit(f"modal app stop {app_id} exited {code} and the app is still running")


def main() -> int:
    app_id = sys.argv[1]
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        if app_stopped(app_id):
            print(f"{app_id} already stopped", flush=True)
            return 0
        verdict = history_verdict(app_id)
        if verdict:
            print(f"HANDOFF {verdict} (from log history)", flush=True)
            stop(app_id)
            return 0
        proc = subprocess.Popen(["modal", "app", "logs", app_id, "--timestamps", "--follow"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None
        verdict = None
        while verdict is None and time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 30)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            if "GPU_TELEMETRY" not in line:  # QA telemetry is every 5 s; keep the Action log readable
                print(line, end="", flush=True)
            verdict = observe(line)
        proc.terminate()
        if verdict:
            print(f"HANDOFF {verdict}", flush=True)
            stop(app_id)
            return 0
        time.sleep(15)
    print("timed out waiting for the old app's QA", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
