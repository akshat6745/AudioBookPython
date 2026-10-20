"""Local setup: Python 3.11, ffmpeg, espeak-ng, pinned StyleTTS2, verified artifacts."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .inference_patch import patch_tree
from .layout import STYLETTS2_COMMIT, STYLETTS2_REPO, STYLETTS_VERSION, styletts_manifest_key, tagger_manifest_key


def tool_help(missing: list[str]) -> str:
    lines = ["missing system tools: " + ", ".join(missing), "Windows: install Ubuntu on WSL2, then use the Linux commands below."]
    lines.append("macOS: brew install " + " ".join(missing))
    lines.append("Debian/Ubuntu: sudo apt-get install " + " ".join("espeak-ng" if item == "espeak-ng" else item for item in missing) + " libopus0")
    lines.append("A Cloudflare API token is not an R2 S3 credential. Create a bucket-scoped access key.")
    return "\n".join(lines)


def require_tools() -> None:
    missing = [name for name in ("ffmpeg", "espeak-ng") if shutil.which(name) is None]
    if sys.version_info < (3, 11):
        missing.append(f"python>=3.11 (running {sys.version.split()[0]})")
    if missing:
        raise SystemExit(tool_help(missing))


def cache_dir() -> Path:
    import os

    root = os.environ.get("AUDIOBOOK_WORKER_CACHE")
    return Path(root) if root else Path.home() / ".cache" / "audiobook-worker"


def ensure_styletts2(dest: Path | None = None) -> Path:
    dest = dest or (cache_dir() / "styletts2-eye-v1" / STYLETTS_VERSION / "StyleTTS2")
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(["git", "clone", STYLETTS2_REPO, str(dest)])
        subprocess.check_call(["git", "-C", str(dest), "checkout", "--detach", STYLETTS2_COMMIT])
    rev = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()
    if rev != STYLETTS2_COMMIT:
        raise SystemExit(f"StyleTTS2 at {dest} is {rev}, expected {STYLETTS2_COMMIT}")
    patch_tree(dest)
    return dest


def setup(store) -> None:
    require_tools()
    import json

    root = cache_dir()
    for key, folder in ((styletts_manifest_key(), root / "styletts2-eye-v1" / STYLETTS_VERSION), (tagger_manifest_key(), root / "emotion-tagger-v1" / "2026-10-20.1")):
        manifest = json.loads(store.get_bytes(key).decode())
        for item in manifest["files"]:
            dest = folder / item["name"]
            if item.get("referenced"):
                dest = folder / item["name"]
            store.download(item["key"], dest, item["sha256"], item["bytes"])
    styletts = root / "styletts2-eye-v1" / STYLETTS_VERSION
    ensure_styletts2(styletts / "StyleTTS2")
    _write_runtime_config(styletts)
    print(f"setup complete in {root}")


def _write_runtime_config(styletts: Path) -> None:
    import yaml

    source = styletts / "checkpoint" / "config_ft.yml"
    if not source.exists():
        source = styletts / "artifacts" / "checkpoint" / "config_ft.yml" if False else styletts / "checkpoint" / "config_ft.yml"
    # Artifacts are stored under their manifest names.
    config_path = styletts / "checkpoint" / "config_ft.yml"
    if not config_path.exists():
        alt = styletts / "checkpoint/config_ft.yml"
        config_path = alt
    text = config_path.read_text()
    data = yaml.safe_load(text)
    data["ASR_path"] = str(styletts / "asr" / "epoch_00080.pth")
    data["ASR_config"] = str(styletts / "asr" / "config.yml")
    data["F0_path"] = str(styletts / "jdc" / "bst.t7")
    data["PLBERT_dir"] = str(styletts / "plbert")
    runtime = styletts / "config.runtime.yml"
    runtime.write_text(yaml.safe_dump(data))
