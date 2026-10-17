#!/usr/bin/env python3
"""Render the emotion-tagged novels to Opus audio on a cloud GPU, reading everything from Cloudflare.

Runs unattended on the GPU box (Beam task queue via beam_app.py, or any Linux CUDA machine). It needs these
environment variables, set as Beam secrets:
  CF_ACCOUNT_ID, D1_DATABASE_ID, CF_API_TOKEN          D1 read access (chapter list and titles)
  R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT_URL, R2_BUCKET_NAME   R2 read/write for the bucket

For each novel it downloads the chapter text and the ensemble emotion tags (published by publish_tags.py),
skips chapters whose audio is already in R2 (manifest.json) or held for review (held.json), renders the rest
with the Whisper check, and uploads per-paragraph audio + manifest.json to
  audio/<novel>/higgs_v3-<narrator>-<dialogue>/chapter_<n>/
A chapter with a clip that never passes the check gets only held.json there, listing the failing clips.
Re-running continues where the last run stopped.

  python3 scripts/gpu_tts/audiobook/cloud_render.py                         # all tagged novels
  python3 scripts/gpu_tts/audiobook/cloud_render.py the-shining --chapters 1-3
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
GPU_TTS = HERE.parent
sys.path[:0] = [str(HERE), str(GPU_TTS)]

import gpu_library_worker as gw  # noqa: E402

NOVELS = [
    "the-shining",
    "haunting-adeline-cat-and-mouse-duet-book-1",
    "the-eye-of-the-world",
    "oathbringer-the-stormlight-archive-book-3",
    "lord-of-the-mysteries",
]
ML_PREFIX = "ml/emotion-2026-10-10"
TAG_MODEL = "deberta3large_teacher2+deberta3_teacher2"
VOICES_DIR = HERE / "voices"
REF_TEXT = GPU_TTS / "texts" / "passage.txt"
SECRETS = ["CF_ACCOUNT_ID", "D1_DATABASE_ID", "CF_API_TOKEN",
           "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME"]
URL_TTL_S = 7 * 24 * 3600  # R2 maximum; a URL is used within minutes of its chapter starting, not of the run


def chapter_list(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        out.update(range(int(lo), int(hi or lo) + 1))
    return out


def d1_query(sql: str, params: list) -> list[dict]:
    url = (f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}"
           f"/d1/database/{os.environ['D1_DATABASE_ID']}/query")
    req = urllib.request.Request(url, data=json.dumps({"sql": sql, "params": params}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    if not data.get("success"):
        raise RuntimeError(f"D1 error: {data.get('errors')}")
    return data["result"][0].get("results", [])


def r2_client():
    import boto3
    from botocore.config import Config

    return boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                        region_name="auto", config=Config(signature_version="s3v4", retries={"max_attempts": 5}))


def list_keys(s3, bucket: str, prefix: str) -> set[str]:
    keys: set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.update(o["Key"] for o in page.get("Contents", []))
    return keys


def encode_voices(narrator: str, dialogue: str) -> dict:
    out = {"narrator_name": narrator, "dialogue_name": dialogue, "ref_text": REF_TEXT.read_text().strip(),
           "wav_b64": {}, "sha256": {}}
    for role, name in (("narrator", narrator), ("dialogue", dialogue)):
        raw = (VOICES_DIR / f"{name}.wav").read_bytes()
        out["wav_b64"][role] = base64.b64encode(raw).decode("ascii")
        out["sha256"][role] = hashlib.sha256(raw).hexdigest()
    return out


def build_job(s3, bucket: str, query, work: Path, novels: list[str], chapters: set[int] | None = None,
              retry_held: bool = False, narrator: str = "default_seed8", dialogue: str = "default_seed2",
              fmt: str = "opus", bitrate: str = "64k", workers: int = 8, temperature: float = 0.8) -> tuple[dict, dict]:
    """The worker's job bundle, with chapter text and per-chapter tags already on disk under work/novels/<novel>.

    Returns (bundle, report); report lists per novel the chapters done, held, skipped for tag mismatches, queued.
    """
    ext = gw.FORMATS[fmt][0]
    sign = lambda op, key, **kw: s3.generate_presigned_url(  # noqa: E731
        op, Params={"Bucket": bucket, "Key": key, **kw}, ExpiresIn=URL_TTL_S)
    tag_keys = list_keys(s3, bucket, f"{ML_PREFIX}/tags/")
    jobs, report = [], {}
    for novel in novels:
        if not re.fullmatch(r"[a-z0-9-]+", novel):
            raise ValueError(f"invalid novel id: {novel!r}")
        tag_key = f"{ML_PREFIX}/tags/{novel}_{TAG_MODEL}.jsonl"
        if tag_key not in tag_keys:
            raise RuntimeError(f"{novel}: no ensemble tags in R2 ({tag_key}); run publish_tags.py first")
        rows = query("SELECT chapter_number, title, r2_content_path FROM chapters "
                     "WHERE novel_id = ? AND r2_content_path IS NOT NULL ORDER BY chapter_number", [novel])
        if chapters is not None:
            rows = [r for r in rows if int(r["chapter_number"]) in chapters]
        prefix = f"audio/{novel}/higgs_v3-{narrator}-{dialogue}"
        existing = list_keys(s3, bucket, f"{prefix}/")
        done = {int(r["chapter_number"]) for r in rows if f"{prefix}/chapter_{r['chapter_number']}/manifest.json" in existing}
        held = {int(r["chapter_number"]) for r in rows
                if f"{prefix}/chapter_{r['chapter_number']}/held.json" in existing} - done
        todo = [r for r in rows if int(r["chapter_number"]) not in done | (set() if retry_held else held)]

        novel_dir = work / "novels" / novel
        (novel_dir / "chapters").mkdir(parents=True, exist_ok=True)
        (novel_dir / "tags").mkdir(parents=True, exist_ok=True)
        tags_by_ch: dict[int, list[dict]] = defaultdict(list)
        for ln in s3.get_object(Bucket=bucket, Key=tag_key)["Body"].read().decode().splitlines():
            if ln.strip():
                row = json.loads(ln)
                tags_by_ch[int(row.pop("chapter"))].append(row)

        def fetch(r: dict) -> tuple[int, int]:
            ch = int(r["chapter_number"])
            path = novel_dir / "chapters" / f"{ch:05d}.txt"
            if not path.exists():
                raw = s3.get_object(Bucket=bucket, Key=r["r2_content_path"])["Body"].read()
                path.write_text(gw.decode_chapter_bytes(raw), encoding="utf-8")
            return ch, len(gw.render_mod.paragraphs(path))

        with ThreadPoolExecutor(16) as pool:
            counts = dict(pool.map(fetch, todo))

        ch_specs, mismatched = [], []
        for r in todo:
            ch = int(r["chapter_number"])
            n = counts[ch]
            if len(tags_by_ch.get(ch, [])) != n:
                mismatched.append(ch)
                continue
            (novel_dir / "tags" / f"{ch:05d}.jsonl").write_text(
                "".join(json.dumps(t) + "\n" for t in sorted(tags_by_ch[ch], key=lambda t: t["paragraph"])))
            ch_prefix = f"{prefix}/chapter_{ch}"
            names = [f"title{ext}", *(f"{i}{ext}" for i in range(n))]
            ch_specs.append({
                "chapter": ch, "title": r.get("title") or f"Chapter {ch}", "paragraph_count": n,
                "get_text_url": sign("get_object", r["r2_content_path"]),
                "r2_prefix": ch_prefix,
                "put_mp3_urls": {f: sign("put_object", f"{ch_prefix}/{f}", ContentType=gw.FORMATS[fmt][2]) for f in names},
                "put_manifest_url": sign("put_object", f"{ch_prefix}/manifest.json", ContentType="application/json"),
                "put_held_url": sign("put_object", f"{ch_prefix}/held.json", ContentType="application/json"),
            })
        report[novel] = {"chapters": len(rows), "done": len(done), "held": sorted(held),
                         "tag_mismatch": mismatched, "queued": len(ch_specs)}
        jobs.append({"novel": novel, "chapters": ch_specs})

    bundle = {
        "version": 1, "phase": "audio",
        "settings": {"variant": "dual", "narrator": narrator, "dialogue": dialogue, "temperature": temperature,
                     "bitrate": bitrate, "format": fmt, "workers": workers, "model": TAG_MODEL},
        "voices": encode_voices(narrator, dialogue),
        "novels": jobs,
    }
    return bundle, report


def start_higgs(cache: Path | None) -> None:
    """Install and start the SGLang Higgs v3 server; the 9 GB model is kept on the cache volume when there is one."""
    models = Path("/workspace/models")
    if cache and not models.exists():
        (cache / "models").mkdir(parents=True, exist_ok=True)
        models.parent.mkdir(parents=True, exist_ok=True)
        models.symlink_to(cache / "models")
    subprocess.run(["bash", str(GPU_TTS / "higgs_v3" / "setup.sh")], check=True)
    subprocess.run(["bash", str(GPU_TTS / "higgs_v3" / "serve.sh")], check=True)


def run(novels: list[str] | None = None, chapters: str | None = None, retry_held: bool = False,
        workers: int = 8, fmt: str = "opus", skip_whisper: bool = False,
        work: Path = Path("/workspace/audiobook_job"), cache: Path | None = None) -> dict:
    missing = [k for k in SECRETS if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing environment variables (Beam secrets): {', '.join(missing)}")
    if cache:
        os.environ.setdefault("HF_HOME", str(cache / "hf"))
    t0 = time.time()
    s3, bucket = r2_client(), os.environ["R2_BUCKET_NAME"]
    bundle, report = build_job(s3, bucket, d1_query, work, novels or NOVELS,
                               chapter_list(chapters) if chapters else None, retry_held, fmt=fmt, workers=workers)
    gw.log(f"job: {json.dumps(report)}")
    result = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)), "report": report}
    if any(n["chapters"] for n in bundle["novels"]):
        start_higgs(cache)
        try:
            result |= gw.run_phase2_audio(bundle, work, "http://127.0.0.1:8000", "sglang", "sglang",
                                          skip_whisper, keep_files=False, novels_filter=None)
        finally:
            subprocess.run(["bash", str(GPU_TTS / "higgs_v3" / "serve.sh"), "stop"], check=False)
    result["hours"] = round((time.time() - t0) / 3600, 2)
    s3.put_object(Bucket=bucket, Key=f"{ML_PREFIX}/render_runs/{result['started']}.json",
                  Body=json.dumps(result, indent=1).encode(), ContentType="application/json")
    gw.log(f"done: {len(result.get('uploaded', []))} chapters uploaded, held: {result.get('held', [])}")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("novels", nargs="*", help=f"default: {' '.join(NOVELS)}")
    p.add_argument("--chapters", help="e.g. 1-3 or 1,5,9")
    p.add_argument("--retry-held", action="store_true", help="re-render chapters previously held for review")
    p.add_argument("--workers", type=int, default=8, help="paragraphs generated at once")
    p.add_argument("--format", default="opus", choices=sorted(gw.FORMATS))
    p.add_argument("--skip-whisper", action="store_true")
    p.add_argument("--work", type=Path, default=Path("/workspace/audiobook_job"))
    p.add_argument("--cache", type=Path, help="persistent directory for the model and Whisper downloads")
    a = p.parse_args()
    print(json.dumps(run(a.novels or None, a.chapters, a.retry_held, a.workers, a.format, a.skip_whisper,
                         a.work, a.cache), indent=1))


if __name__ == "__main__":
    main()
