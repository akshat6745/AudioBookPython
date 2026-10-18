#!/usr/bin/env python3
"""Start the detached Modal render described by render_request.json.

  MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=... python queue_modal_task.py render_request.json
"""
import json
import subprocess
import sys

FUNCTIONS = {"H100": "render_h100"}


def command(request: dict) -> list[str]:
    gpu = request.get("gpu") or "H100"
    if gpu not in FUNCTIONS:
        raise SystemExit(f"gpu must be H100 for this render, got {gpu!r}")
    novel = (request.get("novels") or ["the-shining"])[0]
    cmd = ["modal", "run", "--detach", f"modal_app.py::{FUNCTIONS[gpu]}", f"--novel={novel}"]
    chapters = str(request.get("chapters") or "").strip()
    if chapters:
        cmd.append(f"--chapters={chapters}")
    return cmd


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
