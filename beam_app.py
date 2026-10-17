"""Beam task queue that renders the emotion-tagged novels to audio (scripts/gpu_tts/audiobook/cloud_render.py).

Needs these Beam secrets (Settings -> Secrets): CF_ACCOUNT_ID, D1_DATABASE_ID, CF_API_TOKEN, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, R2_ENDPOINT_URL, R2_BUCKET_NAME. Start a task with a JSON body such as
  {}                                                   every tagged novel, continuing where the last run stopped
  {"novels": ["the-shining"], "chapters": "1-3"}       a small pilot
  {"retry_held": true}                                 also re-render chapters held for review
"""
import sys
from pathlib import Path

from beam import Image, Volume, task_queue

AUDIOBOOK = Path(__file__).resolve().parent / "scripts" / "gpu_tts" / "audiobook"
CACHE = "/workspace/cache"

IMAGE = (
    Image(base_image="lmsysorg/sglang-omni:dev")
    .add_commands(["apt-get update -qq && apt-get install -y -qq ffmpeg curl ca-certificates"])
    .add_python_packages(["boto3", "numpy", "torch", "transformers==5.19.0", "tokenizers==0.23.3",
                          "safetensors==0.8.0", "sentencepiece==0.2.2", "protobuf==7.36.2", "huggingface_hub"])
)


@task_queue(
    name="audiobook-render",
    gpu="RTX4090",
    cpu=8,
    memory="32Gi",
    image=IMAGE,
    volumes=[Volume(name="audiobook-models-cache", mount_path=CACHE)],
    secrets=["CF_ACCOUNT_ID", "D1_DATABASE_ID", "CF_API_TOKEN",
             "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME"],
    timeout=-1,
    retries=0,
)
def render_novels(novels: list[str] | None = None, chapters: str | None = None, retry_held: bool = False,
                  workers: int = 8) -> dict:
    sys.path.insert(0, str(AUDIOBOOK))
    import cloud_render

    return cloud_render.run(novels, chapters, retry_held, workers, cache=Path(CACHE))
