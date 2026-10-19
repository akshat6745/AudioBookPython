#!/usr/bin/env python3
"""Start the detached student-TTS Modal pipeline from student_tts_request.json."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

APP_ID_FILE = Path("student_tts_app_id.txt")  # read by monitor_student_tts.py; detached apps aren't found by name


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
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    app_id = None
    for line in proc.stdout:
        print(line, end="", flush=True)
        match = re.search(r"\b(ap-[A-Za-z0-9]+)\b", line)
        if match and not app_id:
            app_id = match.group(1)
            APP_ID_FILE.write_text(app_id)
    if proc.wait() != 0:
        raise SystemExit(proc.returncode)
    if not app_id:
        raise SystemExit("modal run did not print an app ID; the monitor cannot follow its logs")
    print(f"app id {app_id}")


if __name__ == "__main__":
    main()
