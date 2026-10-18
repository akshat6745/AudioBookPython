#!/usr/bin/env python3
"""Render a novel on one Modal GPU, raising Higgs concurrency while the audio is produced.

The first waves are real chapter audio. Concurrency starts at 2 and rises until the GPU is near full,
more streams stop producing more audio, requests fail, or VRAM is nearly exhausted. Whisper stays off.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import threading
import time
from pathlib import Path

import cloud_render as cr
import gpu_library_worker as gw

CEILING = 16


def next_concurrency(current: int, util: float | None, x_realtime: float, previous: dict | None,
                     errors: int, clips: int, vram_pct: float | None, ceiling: int = CEILING) -> tuple[int, bool]:
    """Return (concurrency for the next wave, settled).

    Sweep 2, 4, 8, 16 and stop only when measured audio throughput falls. SGLang
    reserves most VRAM for its KV cache and an autoregressive decode can report
    near-100% GPU utilization at batch 1, so neither metric is a saturation signal.
    They remain in the trace for diagnosis.
    """
    if clips and errors > clips // 2:
        return previous["concurrency"] if previous else max(1, current // 2), True
    if previous and x_realtime < 0.95 * previous["x_realtime"]:
        return previous["concurrency"], True
    if current >= ceiling:
        return current, True
    nxt = min(ceiling, current * 2)
    return nxt, nxt == current


class GpuWindow:
    """Median GPU utilization and peak VRAM while a wave is generating."""

    def __init__(self, probe=None):
        self._probe = probe or gpu_sample
        self._samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                sample = self._probe()
                if sample:
                    self._samples.append(sample)
                self._stop.wait(1)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def summary(self) -> dict:
        if not self._samples:
            return {"util": None, "vram_pct": None}
        return {"util": round(statistics.median(s["util"] for s in self._samples), 1),
                "vram_pct": round(max(s["vram_pct"] for s in self._samples), 1)}


def gpu_sample() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()[0]
        util, used, total = (float(part.strip()) for part in out.split(","))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return {"util": util, "vram_pct": round(100 * used / total, 1) if total else None}


class Ramp:
    def __init__(self, ceiling: int = CEILING):
        self.workers = 2
        self.settled = False
        self.ceiling = ceiling
        self.trace: list[dict] = []
        self._previous: dict | None = None

    def measure(self) -> GpuWindow:
        return GpuWindow()

    def observe(self, audio_s: float, wall_s: float, errors: int, clips: int, sample: dict) -> int:
        if self.settled:
            return self.workers
        x = round(audio_s / wall_s, 2) if wall_s else 0.0
        nxt, settled = next_concurrency(
            self.workers, sample.get("util"), x, self._previous, errors, clips, sample.get("vram_pct"), self.ceiling)
        self.trace.append({"concurrency": self.workers, "next": nxt, "settled": settled, "x_realtime": x,
                           "errors": errors, "clips": clips, "util": sample.get("util"),
                           "vram_pct": sample.get("vram_pct")})
        self._previous = {"concurrency": self.workers, "x_realtime": x}
        self.workers, self.settled = nxt, settled
        return self.workers


def run_render(novel: str = "the-shining", chapters: str | None = None,
               work: Path = Path("/workspace/audiobook_job"), cache: Path | None = Path("/workspace/cache")) -> dict:
    """Render one novel. chapters is empty for the whole book. Uploaded chapters are skipped on a rerun."""
    missing = [k for k in cr.SECRETS if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing environment variables (Modal secret cloudflare-audiobook): {', '.join(missing)}")
    if cache:
        os.environ.setdefault("HF_HOME", str(cache / "hf"))
    selected = cr.chapter_list(chapters) if chapters else None
    t0 = time.time()
    s3, bucket = cr.r2_client(), os.environ["R2_BUCKET_NAME"]
    bundle, job = cr.build_job(s3, bucket, cr.d1_query, work, [novel], selected, fmt="opus", workers=2)
    ramp = Ramp()
    result = {"novel": novel, "chapters": chapters or "all", "gpu": os.environ.get("MODAL_GPU", "L4"),
              "qa": "pending-whisper", "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
              "job": job, "uploaded": [], "held": []}

    def publish(summary: dict | None = None) -> None:
        if summary:
            result["uploaded"] = list(summary.get("uploaded", []))
            result["held"] = list(summary.get("held", []))
        result["concurrency"] = ramp.workers
        result["settled"] = ramp.settled
        result["trace"] = ramp.trace
        result["hours"] = round((time.time() - t0) / 3600, 2)
        body = json.dumps(result, indent=1).encode()
        s3.put_object(Bucket=bucket, Key=f"{cr.ML_PREFIX}/render_runs/{novel}-adaptive.json",
                      Body=body, ContentType="application/json")

    if any(n["chapters"] for n in bundle["novels"]):
        try:
            cr.start_higgs(cache)
            summary = gw.run_phase2_audio(
                bundle, work, "http://127.0.0.1:8000", "sglang", "sglang",
                skip_whisper=True, keep_files=False, novels_filter=None, ramp=ramp, on_chapter=publish)
            result["uploaded"] = summary["uploaded"]
            result["held"] = summary["held"]
        finally:
            subprocess.run(["bash", str(cr.GPU_TTS / "higgs_v3" / "serve.sh"), "stop"], check=False)
            publish()
    else:
        publish()
    gw.log(f"render report -> {cr.ML_PREFIX}/render_runs/{novel}-adaptive.json "
           f"concurrency {ramp.workers} uploaded {len(result['uploaded'])}")
    return result
