"""Modal benchmark for Higgs v3 concurrency. Whisper is intentionally not run here.

The Cloudflare values come from the Modal secret `cloudflare-audiobook`. GitHub Actions starts one of:

  modal run --detach modal_app.py::benchmark_l40s --chapters=1-3 --levels=8,16,32,48,64
  modal run --detach modal_app.py::benchmark_l4 --chapters=1-3 --levels=8,16,32,48,64

The report is written to R2 at ml/emotion-2026-10-10/render_runs/benchmark-<time>.json.
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

app = modal.App("audiobook-benchmark", image=image)


def _run(gpu: str, chapters: str, levels: str, novel: str) -> dict:
    os.environ["MODAL_GPU"] = gpu
    sys.path[:0] = [f"{GPU_TTS}/audiobook", GPU_TTS]
    import modal_bench

    result = modal_bench.run_benchmark(novel, chapters, levels, cache=Path(CACHE))
    VOLUME.commit()
    return result


@app.function(gpu="L40S", cpu=8, memory=32768, timeout=3 * 3600, secrets=[SECRET], volumes={CACHE: VOLUME})
def benchmark_l40s(chapters: str = "1-3", levels: str = "8,16,32,48,64", novel: str = "the-shining") -> dict:
    return _run("L40S", chapters, levels, novel)


@app.function(gpu="L4", cpu=8, memory=32768, timeout=3 * 3600, secrets=[SECRET], volumes={CACHE: VOLUME})
def benchmark_l4(chapters: str = "1-3", levels: str = "8,16,32,48,64", novel: str = "the-shining") -> dict:
    return _run("L4", chapters, levels, novel)
