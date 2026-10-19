#!/usr/bin/env python3
"""Start the detached student-TTS Modal pipeline from student_tts_request.json."""
from __future__ import annotations

import json
import subprocess
import sys


def command(request: dict) -> list[str]:
    gpu = (request.get("gpu") or "l40s").lower()
    if gpu not in {"l40s", "l4"}:
        raise SystemExit(f"gpu must be l40s or l4, got {gpu!r}")
    return [
        "modal",
        "run",
        "--detach",
        "modal_student_tts.py::pipeline",
        f"--gpu={gpu}",
        "--confirm-spend",
    ]


def main() -> None:
    request = json.load(open(sys.argv[1]))
    if request.get("run") is not True:
        print("run is not true; nothing queued")
        return
    cmd = command(request)
    print("starting", " ".join(cmd))
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
