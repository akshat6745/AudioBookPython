#!/usr/bin/env python3
"""Find how many Higgs requests one Modal GPU should run at once.

Generates the same tagged clips at each concurrency, discards the audio, and writes the timing report to R2.
It does not upload chapter audio or a manifest. Whisper stays off; that check runs later on the Mac.

  python3 modal_bench.py --chapters 1-3 --levels 8,16,32,48,64
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cloud_render as cr
import gpu_library_worker as gw
import higgs_format as hf
import render

CLIP_LIMIT = 40  # one fixed set, repeated at every concurrency, so the comparison is fair


def collect_clips(texts: dict[int, list[str]], tags: dict[int, list[dict]], limit: int = CLIP_LIMIT) -> list[tuple[str, str]]:
    """(voice, Higgs request) pairs in chapter order, capped so a slow GPU cannot run for hours."""
    clips: list[tuple[str, str]] = []
    cuts = hf.Cuts()
    for ch in sorted(texts):
        for chunks in render.plan(texts[ch], tags[ch], "dual", cuts):
            clips.extend(chunks)
            if len(clips) >= limit:
                return clips[:limit]
    return clips


def keep_going(results: list[dict]) -> bool:
    """False when half the clips failed, or this level is under 95% of the best level so far."""
    row = results[-1]
    if row["errors"] > row["clips"] // 2:
        return False
    earlier = [r["x_realtime"] for r in results[:-1]]
    return not earlier or row["x_realtime"] >= 0.95 * max(earlier)


def sweep(clips: list[tuple[str, str]], levels: list[int], generate, now=time.perf_counter) -> dict:
    """generate(voice, text) -> seconds of audio. Stop once keep_going() says the GPU is saturated."""
    results = []
    for n in levels:
        t0 = now()
        audio_s, errors = 0.0, 0
        with ThreadPoolExecutor(n) as pool:
            futures = [pool.submit(generate, voice, text) for voice, text in clips]
            for future in futures:
                try:
                    audio_s += float(future.result())
                except Exception:
                    errors += 1
        wall_s = now() - t0
        results.append({"concurrency": n, "clips": len(clips), "errors": errors, "audio_s": round(audio_s, 1),
                        "wall_s": round(wall_s, 1), "x_realtime": round(audio_s / wall_s, 2) if wall_s else 0.0})
        if not keep_going(results):
            break
    best = max(results, key=lambda r: (r["x_realtime"], -r["errors"]))
    return {"levels": results, "best_concurrency": best["concurrency"], "best_x_realtime": best["x_realtime"]}


def parse_levels(spec: str) -> list[int]:
    levels = [int(part) for part in spec.split(",") if part.strip()]
    if not levels or any(n < 1 for n in levels):
        raise ValueError(f"levels must be positive integers, got {spec!r}")
    return levels


def load_chapters(work: Path, novel: str, chapters: list[int]) -> tuple[dict[int, list[str]], dict[int, list[dict]]]:
    novel_dir = work / "novels" / novel
    texts, tags = {}, {}
    for ch in chapters:
        texts[ch] = render.paragraphs(novel_dir / "chapters" / f"{ch:05d}.txt")
        tags[ch] = [json.loads(ln) for ln in (novel_dir / "tags" / f"{ch:05d}.jsonl").read_text().splitlines() if ln.strip()]
    return texts, tags


def run_benchmark(novel: str = "the-shining", chapters: str = "1-3", levels: str = "8,16,32,48,64",
                  work: Path = Path("/workspace/audiobook_job"), cache: Path | None = Path("/workspace/cache")) -> dict:
    """Start Higgs once, time each concurrency, and store the report under ml/.../render_runs/."""
    missing = [k for k in cr.SECRETS if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing environment variables (Modal secret cloudflare-audiobook): {', '.join(missing)}")
    if cache:
        os.environ.setdefault("HF_HOME", str(cache / "hf"))
    selected = parse_levels(levels)
    chapter_ids = sorted(cr.chapter_list(chapters))
    t0 = time.time()
    s3, bucket = cr.r2_client(), os.environ["R2_BUCKET_NAME"]
    bundle, _report = cr.build_job(s3, bucket, cr.d1_query, work, [novel], set(chapter_ids), fmt="opus", workers=1)
    gw.ensure_voices(bundle, work)
    texts, tags = load_chapters(work, novel, chapter_ids)
    clips = collect_clips(texts, tags)
    if not clips:
        raise RuntimeError(f"no clips for {novel} chapters {chapters}")

    cr.start_higgs(cache)
    import subprocess

    import synth
    try:
        ref = (work / "voices" / "ref_text.txt").read_text().strip()
        voices = {"narrator": (str(work / "voices" / "narrator.wav"), ref),
                  "dialogue": (str(work / "voices" / "dialogue.wav"), ref)}
        tts = synth.backend("sglang", url="http://127.0.0.1:8000", voices=voices, sampling={"temperature": 0.8})

        def generate(voice: str, text: str) -> float:
            _audio, _sr, info = tts.generate(text, seed=0, voice=voice)
            return float(info["audio_s"])

        generate(*clips[0])  # warmup; not included in the level timings
        result = sweep(clips, selected, generate)
    finally:
        subprocess.run(["bash", str(cr.GPU_TTS / "higgs_v3" / "serve.sh"), "stop"], check=False)

    result.update(novel=novel, chapters=chapters, clips=len(clips), gpu_name=os.environ.get("MODAL_GPU", ""),
                  started=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
                  hours=round((time.time() - t0) / 3600, 2))
    key = f"{cr.ML_PREFIX}/render_runs/benchmark-{result['started']}.json"
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(result, indent=1).encode(), ContentType="application/json")
    gw.log(f"benchmark report -> {key}: best {result['best_concurrency']} at {result['best_x_realtime']}x")
    return result
