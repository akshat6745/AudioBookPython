"""Persist a parsed EPUB: novel row → D1, chapter text → R2, cover → R2.

Shared by the /upload-epub endpoint and scripts/upload_public_domain_novels.py
(which runs this locally so bulk imports never load the production server).
"""

import asyncio
import re
from typing import Optional

import structlog

from app.core.d1_client import get_d1_client
from app.services.cloudflare_service import upload_chapter_text_to_r2, _r2_client, _r2_bucket

logger = structlog.get_logger()


def generate_slug(title: str) -> str:
    """Generate a URL-friendly slug from a title."""
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    return slug[:200] if len(slug) > 200 else slug


def calculate_word_count(content: list) -> int:
    return sum(len(p.split()) for p in content)


def cover_r2_key(novel_slug: str) -> str:
    return f"covers/{novel_slug}.png"


async def store_novel(
    novel,
    chapters: list,
    cover_image: Optional[bytes],
    *,
    user_id: Optional[str],
    is_public: bool,
) -> Optional[str]:
    """Store a parsed novel and return its slug, or None if it already exists.

    `user_id` is None for unowned novels (e.g. the public-domain catalogue);
    `is_public` makes the novel visible in every user's library.
    """
    d1 = get_d1_client()
    slug = generate_slug(novel.title)

    # ── Check if novel already exists in D1 ───────────────────────────────
    existing = await d1.query(
        "SELECT id, total_chapters FROM novels WHERE id = ?", [slug]
    )
    if existing and existing[0].get("total_chapters", 0) > 0:
        return None

    # Make slug unique if collision
    base_slug = slug
    counter = 1
    while True:
        check = await d1.query("SELECT id FROM novels WHERE id = ?", [slug])
        if not check:
            break
        slug = f"{base_slug}-{counter}"
        counter += 1

    # ── Insert novel into D1 ──────────────────────────────────────────────
    await d1.execute(
        """
        INSERT OR REPLACE INTO novels
            (id, title, author, language, total_chapters, user_id, is_public, created_at, updated_at)
        VALUES (?, ?, ?, 'en', ?, ?, ?, datetime('now'), datetime('now'))
        """,
        [slug, novel.title, novel.author or "Unknown", len(chapters), user_id, int(is_public)],
    )
    logger.info("Inserted novel into D1", slug=slug, chapters=len(chapters))

    # ── Upload each chapter: text → R2, metadata → D1 ────────────────────
    # Bound concurrency: R2 uploads are sync (boto3) and run in the executor
    # thread pool, D1 inserts are async HTTP. Cap at 20 to avoid exhausting
    # either pool while still giving a large speedup over serial.
    chapter_sem = asyncio.Semaphore(20)
    loop = asyncio.get_running_loop()

    async def upload_chapter(chapter):
        chap_num     = chapter["chapterNumber"]
        chap_title   = chapter.get("chapterTitle", f"Chapter {chap_num}")
        chap_content = chapter.get("content", [])
        word_count   = calculate_word_count(chap_content)
        text         = "\n\n".join(p.strip() for p in chap_content if p.strip())

        async with chapter_sem:
            r2_key = await loop.run_in_executor(
                None, upload_chapter_text_to_r2, slug, chap_num, text
            )
            await d1.execute(
                """
                INSERT OR REPLACE INTO chapters
                    (id, novel_id, chapter_number, title,
                     r2_content_path, word_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                [f"{slug}_ch_{chap_num}", slug, chap_num, chap_title, r2_key, word_count],
            )

    await asyncio.gather(*(upload_chapter(c) for c in chapters))

    logger.info("Uploaded chapters to R2 + D1", slug=slug, count=len(chapters))

    # ── Upload cover image to R2 if extracted from EPUB ──────────────
    if cover_image:
        try:
            r2_key = cover_r2_key(slug)
            _r2_client().put_object(
                Bucket=_r2_bucket(),
                Key=r2_key,
                Body=cover_image,
                ContentType="image/jpeg",
            )
            await d1.execute(
                "UPDATE novels SET cover_url = ? WHERE id = ?",
                [r2_key, slug],
            )
            logger.info("Uploaded EPUB cover to R2", novel=slug)
        except Exception as cover_err:
            logger.warning("Failed to upload EPUB cover", error=str(cover_err))

    return slug
