#!/usr/bin/env python3
"""Publish locally made emotion tags to R2 so a GPU box can render audio without re-tagging.

Every chapter's tag count is checked against the chapter text in R2 (split exactly like the app), so the GPU
run never pairs a paragraph with another paragraph's tags. Run from the repo root on the Mac:

  .venv/bin/python scripts/gpu_tts/audiobook/publish_tags.py the-shining lord-of-the-mysteries
  .venv/bin/python scripts/gpu_tts/audiobook/publish_tags.py --check-only the-shining
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(HERE), str(REPO)]

from app.core.d1_client import D1Client  # noqa: E402
from app.services.cloudflare_service import _r2_bucket, _r2_client, read_chapter_text_from_r2  # noqa: E402

TAGS_DIR = HERE.parent / "emotion_eval" / "data" / "tags"
ML_PREFIX = "ml/emotion-2026-10-10"
TAG_MODEL = "deberta3large_teacher2+deberta3_teacher2"


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


async def chapter_rows(novel: str) -> list[dict]:
    d1 = D1Client()
    try:
        return await d1.query("SELECT chapter_number, r2_content_path FROM chapters "
                              "WHERE novel_id = ? AND r2_content_path IS NOT NULL ORDER BY chapter_number", [novel])
    finally:
        await d1.close()


def mismatches(novel: str, tag_counts: Counter) -> list[str]:
    rows = asyncio.run(chapter_rows(novel))

    def count(r: dict) -> tuple[int, int]:
        return int(r["chapter_number"]), len(split_paragraphs(read_chapter_text_from_r2(r["r2_content_path"])))

    with ThreadPoolExecutor(16) as pool:
        text_counts = dict(pool.map(count, rows))
    problems = [f"ch{ch}: {text_counts.get(ch)} paragraphs in R2, {tag_counts.get(ch)} tags"
                for ch in sorted(set(text_counts) | set(tag_counts)) if text_counts.get(ch) != tag_counts.get(ch)]
    print(f"{novel}: {len(text_counts)} chapters in D1, {len(tag_counts)} tagged, {len(problems)} mismatched")
    return problems


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("novels", nargs="+")
    p.add_argument("--model", default=TAG_MODEL)
    p.add_argument("--check-only", action="store_true")
    a = p.parse_args()

    s3, bucket = _r2_client(), _r2_bucket()
    failed = False
    for novel in a.novels:
        path = TAGS_DIR / f"{novel}_{a.model}.jsonl"
        raw = path.read_bytes()
        tag_counts = Counter(json.loads(ln)["chapter"] for ln in raw.decode().splitlines() if ln.strip())
        problems = mismatches(novel, tag_counts)
        for line in problems[:10]:
            print("   ", line)
        if problems:
            failed = True
            continue
        if not a.check_only:
            key = f"{ML_PREFIX}/tags/{path.name}"
            s3.put_object(Bucket=bucket, Key=key, Body=raw, ContentType="application/x-ndjson")
            print(f"    uploaded {key} ({len(raw) / 1e6:.1f} MB)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
