"""Budget-capped Modal L40S render for the five emotion-tagged novels.

The Cloudflare values come from the Modal secret `cloudflare-audiobook`. GitHub Actions starts:

  modal run --detach modal_app.py::queue_l40s --novels=the-shining,...

Progress is written to R2 at ml/emotion-2026-10-10/render_runs/five-novels-l40s-adaptive.json.
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


@app.function(gpu="L40S", cpu=4, memory=16384, timeout=7 * 3600 + 50 * 60,
              secrets=[SECRET], volumes={CACHE: VOLUME})
def render_l40s(novels: str = "the-shining", chapters: str = "") -> dict:
    os.environ["MODAL_GPU"] = "L40S"
    sys.path[:0] = [f"{GPU_TTS}/audiobook", GPU_TTS]
    import modal_bench

    selected = [novel.strip() for novel in novels.split(",") if novel.strip()]
    result = modal_bench.run_render(selected, chapters or None, cache=Path(CACHE))
    VOLUME.commit()
    return result


@app.local_entrypoint()
def queue_l40s(novels: str = "the-shining", chapters: str = "") -> None:
    """Submit one durable L40S function call and return without waiting."""
    call = render_l40s.spawn(novels, chapters)
    print(f"spawned Modal function call {call.object_id}")
