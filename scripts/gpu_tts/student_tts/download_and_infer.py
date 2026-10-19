"""Download the final R2 artifact and optionally synthesize local Mac samples.

Required environment variables are the existing R2_* values; none are logged.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.config import Config

PREFIX = "student_tts/styletts2-eye-v1/checkpoint/"
REQUIRED = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME")


def client():
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise SystemExit("missing R2 environment variables: " + ", ".join(missing))
    endpoint = os.environ["R2_ENDPOINT_URL"]
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("R2_ENDPOINT_URL must be a valid HTTPS endpoint")
    return boto3.client(
        "s3", endpoint_url=endpoint, region_name="auto",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("student_tts_checkpoint"))
    parser.add_argument("--styletts2", type=Path, help="local StyleTTS2 clone; omit to download only")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    s3, bucket = client(), os.environ["R2_BUCKET_NAME"]
    objects = s3.list_objects_v2(Bucket=bucket, Prefix=PREFIX).get("Contents", [])
    keys = [obj["Key"] for obj in objects]
    checkpoints = sorted(key for key in keys if Path(key).name.startswith("epoch_2nd_") and key.endswith(".pth"))
    if not checkpoints or f"{PREFIX}config_ft.yml" not in keys:
        raise SystemExit("final checkpoint/config not found in R2")
    checkpoint, config = out / Path(checkpoints[-1]).name, out / "config_ft.yml"
    s3.download_file(bucket, checkpoints[-1], str(checkpoint))
    s3.download_file(bucket, f"{PREFIX}config_ft.yml", str(config))
    print(f"downloaded {checkpoint} and {config}")
    if args.styletts2:
        sample = Path(__file__).with_name("sample_styletts2.py")
        subprocess.run(
            [sys.executable, str(sample), "--repo", str(args.styletts2.resolve()),
             "--config", str(config), "--checkpoint", str(checkpoint),
             "--output", str(out / "samples")],
            check=True,
        )


if __name__ == "__main__":
    main()
