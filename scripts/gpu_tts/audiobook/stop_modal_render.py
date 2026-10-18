#!/usr/bin/env python3
"""Stop a live audiobook-render Modal app without touching any other app."""
from __future__ import annotations

import json
import subprocess
import sys

ACTIVE = {"ephemeral (detached)", "ephemeral", "initializing...", "deployed"}


def listed_apps() -> list[dict]:
    raw = subprocess.check_output(["modal", "app", "list", "--json"], text=True)
    return json.loads(raw[raw.find("[") :])


def audiobook_apps(apps: list[dict]) -> list[dict]:
    return [app for app in apps if app.get("description") == "audiobook-render"]


def show(prefix: str, app: dict) -> None:
    print(
        f"{prefix} {app.get('app_id')} state={app.get('state')} "
        f"tasks={app.get('tasks')} created={app.get('created_at')} "
        f"stopped={app.get('stopped_at')}"
    )


def main() -> int:
    named = audiobook_apps(listed_apps())
    if not named:
        print("no audiobook-render app is listed")
        return 2
    active = [app for app in named if app.get("state") in ACTIVE]
    for app in active:
        show("stopping", app)
        subprocess.run(["modal", "app", "stop", str(app["app_id"]), "--yes"], check=True)
    remaining = [app for app in audiobook_apps(listed_apps()) if app.get("state") in ACTIVE]
    for app in remaining:
        show("still-running", app)
    if remaining:
        return 1
    print(f"confirmed no audiobook-render container is active; stopped {len(active)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
