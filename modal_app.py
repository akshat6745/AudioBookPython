"""Modal H100 render. Concurrency rises while real chapter audio is produced. Whisper stays on the Mac.

The Cloudflare values come from the Modal secret `cloudflare-audiobook`. GitHub Actions starts:

  modal run --detach modal_app.py::render_h100 --novel=the-shining

Progress is written to R2 at ml/emotion-2026-10-10/render_runs/<novel>-adaptive.json.
A rerun skips chapters whose manifest is already in R2.
"""
import os
import sys
from pathlib import Path

import modal

GPU_TTS = "/root/gpu_tts"
CACHE = "/workspace/cache"
SECRET = modal.Secret.from_name("cloudflare-audiobook")
VOLUME = modal.Volume.from_name("audiobook-models", create_if_missing=True)

image = (
    modal.Image.from_registry("lmsysorg/sglang-omni:dev", add_python="3.11")
    .apt_install("ffmpeg", "curl", "ca-certificates")
    .pip_install("boto3", "numpy")
    .add_local_dir("scripts/gpu_tts", remote_path=GPU_TTS, ignore=["**/__pycache__/**"])
)

app = modal.App("audiobook-render", image=image)


@app.function(gpu="H100", cpu=8, memory=32768, timeout=24 * 3600, secrets=[SECRET], volumes={CACHE: VOLUME})
def render_h100(novel: str = "the-shining", chapters: str = "") -> dict:
    os.environ["MODAL_GPU"] = "H100"
    sys.path[:0] = [f"{GPU_TTS}/audiobook", GPU_TTS]
    import modal_bench

    result = modal_bench.run_render(novel, chapters or None, cache=Path(CACHE))
    VOLUME.commit()
    return result
