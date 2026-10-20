"""Machine-local batch profile. Keyed by stage, device, and model version. Not a cross-laptop lock."""

from __future__ import annotations

import json
import os
from pathlib import Path


def profile_path() -> Path:
    explicit = os.environ.get("AUDIOBOOK_WORKER_STATE")
    if explicit:
        return Path(explicit) / "batch-profile.json"
    return Path.home() / ".local" / "state" / "audiobook-worker" / "batch-profile.json"


def load_profile(path: Path | None = None) -> dict:
    target = path or profile_path()
    if not target.exists():
        return {}
    return json.loads(target.read_text())


_PROFILE_LOCK = __import__("threading").Lock()


def save_effective(stage: str, device: str, model_key: str, batch: int, path: Path | None = None) -> None:
    target = path or profile_path()
    with _PROFILE_LOCK:
        _save_effective(stage, device, model_key, batch, target)


def _save_effective(stage: str, device: str, model_key: str, batch: int, target: Path) -> None:
    data = load_profile(target)
    data.setdefault(stage, {}).setdefault(device, {})[model_key] = int(batch)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    temporary.replace(target)


def work_dir(run_id: str, worker_index: int, root: Path | None = None) -> Path:
    base = root or profile_path().parent
    path = base / "runs" / run_id / f"w{worker_index}"
    path.mkdir(parents=True, exist_ok=True)
    return path
