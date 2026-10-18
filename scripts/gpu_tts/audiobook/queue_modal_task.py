#!/usr/bin/env python3
"""Start the detached Modal benchmark described by render_request.json.

  MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=... python queue_modal_task.py render_request.json
"""
import json
import subprocess
import sys

FUNCTIONS = {"L40S": "benchmark_l40s", "L4": "benchmark_l4"}


def command(request: dict) -> list[str]:
    gpu = request.get("gpu") or "L40S"
    if gpu not in FUNCTIONS:
        raise SystemExit(f"gpu must be one of {', '.join(FUNCTIONS)}, got {gpu!r}")
    novel = (request.get("novels") or ["the-shining"])[0]
    levels = request.get("levels") or [8, 16, 32, 48, 64]
    return [
        "modal", "run", "--detach", f"modal_app.py::{FUNCTIONS[gpu]}",
        f"--chapters={request.get('chapters') or '1-3'}",
        f"--levels={','.join(str(n) for n in levels)}",
        f"--novel={novel}",
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
