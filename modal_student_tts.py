"""Two-voice Higgs -> Whisper QA -> StyleTTS2 pipeline on Modal.

GitHub Actions starts it from student_tts_request.json on branch student-tts-modal:

  modal run --detach modal_student_tts.py::pipeline --gpu l40s --confirm-spend

The local entrypoint only spawns one CPU orchestrator, which runs the stages in order:
select (CPU) -> Higgs generation (L40S) -> Whisper QA (L4) -> StyleTTS2 training (L40S or L4).
Every stage keeps its state on the student-tts-eye-v1 Volume, so a retry resumes where it stopped.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import modal

ROOT = "/root/gpu_tts"
CACHE = Path("/workspace/cache")
WORK = CACHE / "student_tts"
HIGGS_CACHE = "/workspace/higgs_cache"
R2_PREFIX = "student_tts/styletts2-eye-v1"
SECRET = modal.Secret.from_name("cloudflare-audiobook")
VOLUME = modal.Volume.from_name("student-tts-eye-v1", create_if_missing=True)
HIGGS_MODELS = modal.Volume.from_name("audiobook-models", create_if_missing=True)
IGNORE = ["**/__pycache__/**", "**/outputs/**", "**/logs/**", "**/data/**"]

base_image = (
    modal.Image.from_registry("nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04", add_python="3.11")
    .apt_install("ffmpeg", "espeak-ng", "libsndfile1", "git", "build-essential", "clang")
    .pip_install(
        "boto3>=1.35", "numpy>=2.0,<2.3", "soundfile>=0.12", "pyyaml>=6.0", "phonemizer>=3.3",
        "torch==2.7.1", "torchaudio==2.7.1", "transformers>=4.53,<5", "accelerate>=1.8",
        "huggingface_hub>=0.33",
    )
)
cpu_image = base_image.add_local_dir("scripts/gpu_tts", remote_path=ROOT, ignore=IGNORE)
higgs_image = (
    modal.Image.from_registry("lmsysorg/sglang-omni:dev", add_python="3.11")
    .apt_install("ffmpeg", "curl", "ca-certificates")
    .pip_install("boto3", "numpy")
    .add_local_dir("scripts/gpu_tts", remote_path=ROOT, ignore=IGNORE)
)
train_image = (
    base_image.run_commands(
        "git clone https://github.com/yl4579/StyleTTS2.git /opt/styletts2",
        "cd /opt/styletts2 && git checkout 5cedc71c333f8d8b8551ca59378bdcc7af4c9529",
        "python -m pip install munch pydub librosa nltk matplotlib einops einops-exts tqdm "
        "tensorboard git+https://github.com/resemble-ai/monotonic_align.git",
    )
    .add_local_dir("scripts/gpu_tts", remote_path=ROOT, ignore=IGNORE)
)

app = modal.App("student-tts-eye-v1")
gpu_retry = modal.Retries(max_retries=2, backoff_coefficient=2.0, initial_delay=10.0)
VOLUMES = {str(CACHE): VOLUME}


def _path_setup() -> None:
    sys.path[:0] = [f"{ROOT}/audiobook", f"{ROOT}/student_tts", ROOT]


def _with_commits(fn, *args):
    """Commit the Volume every 5 minutes so a preempted container loses little work."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(300):
            VOLUME.commit()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        return fn(*args)
    finally:
        stop.set()
        thread.join(timeout=10)
        VOLUME.commit()


@app.function(image=cpu_image, cpu=8, memory=16384, timeout=2 * 3600, retries=gpu_retry,
              secrets=[SECRET], volumes=VOLUMES)
def select_dataset() -> dict:
    """Fetch chapter text + tags from R2/D1 and write the request manifest (no GPU)."""
    _path_setup()
    import pipeline_worker

    return _with_commits(pipeline_worker.select, WORK)


@app.function(image=higgs_image, gpu="L40S", cpu=8, memory=32768, timeout=12 * 3600, retries=gpu_retry,
              secrets=[SECRET], volumes={**VOLUMES, HIGGS_CACHE: HIGGS_MODELS})
def generate_dataset() -> dict:
    """Higgs only; raw 24 kHz WAVs go to the Volume. Whisper runs later on its own GPU."""
    os.environ["HIGGS_WIDTHS"] = "64 32"  # 64 is the proven L40S width; 128 fails CUDA graph capture
    _path_setup()
    import pipeline_worker

    return _with_commits(pipeline_worker.generate, WORK, Path(HIGGS_CACHE))


@app.function(image=cpu_image, gpu="L4", cpu=8, memory=32768, timeout=8 * 3600, retries=gpu_retry,
              secrets=[SECRET], volumes=VOLUMES)
def whisper_qa() -> dict:
    _path_setup()
    import pipeline_worker

    return _with_commits(pipeline_worker.qa_and_publish, WORK, R2_PREFIX)


def _train(gpu_kind: str) -> dict:
    _path_setup()
    import train_worker

    return _with_commits(train_worker.train, WORK, CACHE, R2_PREFIX, gpu_kind)


@app.function(image=train_image, gpu="L40S", cpu=16, memory=65536, timeout=24 * 3600, retries=gpu_retry,
              secrets=[SECRET], volumes=VOLUMES)
def train_l40s() -> dict:
    return _train("L40S")


@app.function(image=train_image, gpu="L4", cpu=16, memory=65536, timeout=24 * 3600, retries=gpu_retry,
              secrets=[SECRET], volumes=VOLUMES)
def train_l4() -> dict:
    return _train("L4")


@app.function(image=cpu_image, cpu=0.25, memory=1024, timeout=24 * 3600)
def orchestrate(gpu: str) -> dict:
    result = {}
    for name, stage in (("selection", select_dataset), ("generation", generate_dataset), ("qa", whisper_qa),
                        ("training", train_l40s if gpu == "l40s" else train_l4)):
        print(f"STAGE start {name}", flush=True)
        result[name] = stage.remote()
        print(f"STAGE done {name}: {json.dumps(result[name])[:2000]}", flush=True)
    return result


@app.local_entrypoint()
def pipeline(gpu: str = "l40s", confirm_spend: bool = False) -> None:
    if not confirm_spend:
        raise SystemExit("DISARMED: pass --confirm-spend only after reviewing the cost estimate")
    if gpu not in {"l40s", "l4"}:
        raise SystemExit("--gpu must be l40s or l4")
    call = orchestrate.spawn(gpu)
    print(f"spawned Modal function call {call.object_id}")
