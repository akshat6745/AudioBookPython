"""
Migration script: Supabase → Cloudflare D1 + R2  (PARALLEL version)

Uses ThreadPoolExecutor (20 workers) to upload R2 + insert D1 in parallel.
Safe to re-run — INSERT OR IGNORE skips already-migrated chapters.

Usage:
    python scripts/migrate_to_cloudflare.py
"""

import os
import gzip
import time
import threading
import httpx
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
CF_ACCOUNT_ID  = os.environ["CF_ACCOUNT_ID"]
CF_API_TOKEN   = os.environ["CF_API_TOKEN"]
D1_DATABASE_ID = os.environ["D1_DATABASE_ID"]
R2_ENDPOINT    = os.environ["R2_ENDPOINT_URL"]
R2_BUCKET      = os.environ["R2_BUCKET_NAME"]

D1_QUERY_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
    f"/d1/database/{D1_DATABASE_ID}/query"
)
D1_HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json",
}

MAX_WORKERS = 20    # parallel upload threads
PAGE_SIZE   = 200   # chapters fetched per Supabase request

# ── Thread-local clients (each thread gets its own boto3 + httpx) ─────────────
# This avoids sharing state across threads.

_local = threading.local()

def get_r2():
    if not hasattr(_local, "r2"):
        _local.r2 = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
    return _local.r2

def get_http():
    if not hasattr(_local, "http"):
        _local.http = httpx.Client(timeout=30)
    return _local.http

# ── Helpers ───────────────────────────────────────────────────────────────────

def compress(content) -> bytes:
    if isinstance(content, list):
        text = "\n\n".join(p.strip() for p in content if p and p.strip())
    elif isinstance(content, str):
        text = content
    else:
        text = str(content)
    return gzip.compress(text.encode("utf-8"), compresslevel=9)


def d1_execute(sql: str, params: list = None) -> dict:
    payload = {"sql": sql, "params": params or []}
    resp = get_http().post(D1_QUERY_URL, headers=D1_HEADERS, json=payload)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 error: {data.get('errors')}")
    return data["result"][0].get("meta", {})


# ── Per-chapter work (runs in thread pool) ─────────────────────────────────────

def migrate_chapter(ch: dict, id_to_slug: dict) -> tuple[bool, str]:
    """
    Upload one chapter to R2 and insert its metadata into D1.
    Returns (was_new, label).
    """
    novel_slug = id_to_slug.get(str(ch["novel_id"]), str(ch["novel_id"]))
    chap_num   = int(ch["chapter_number"])
    content    = ch.get("content") or []
    r2_key     = f"novels/{novel_slug}/chapter_{chap_num}.txt.gz"
    compressed = compress(content)
    word_count = int(ch.get("word_count") or 0)

    # 1. R2 upload (always overwrite — idempotent)
    get_r2().put_object(
        Bucket=R2_BUCKET,
        Key=r2_key,
        Body=compressed,
        ContentType="text/plain",
        ContentEncoding="gzip",
    )

    # 2. D1 insert
    chapter_id = f"{novel_slug}_ch_{chap_num}"
    meta = d1_execute(
        """
        INSERT OR IGNORE INTO chapters
            (id, novel_id, chapter_number, title,
             r2_content_path, word_count, file_size_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            chapter_id,
            novel_slug,
            chap_num,
            ch.get("chapter_title") or f"Chapter {chap_num}",
            r2_key,
            word_count,
            len(compressed),
        ],
    )
    was_new = meta.get("rows_written", 0) > 0
    return was_new, f"{novel_slug} ch.{chap_num}"

# ── Step 1: Migrate Novels ────────────────────────────────────────────────────

def migrate_novels(sb: Client) -> dict[str, str]:
    print("\n─────────────────────────────────────────────")
    print("  📚 Migrating novels → D1")
    print("─────────────────────────────────────────────")
    novels = sb.table("novels").select("*").execute().data
    print(f"  Found {len(novels)} novels\n")
    id_to_slug: dict[str, str] = {}
    for novel in novels:
        slug = novel.get("slug") or str(novel["id"])
        id_to_slug[str(novel["id"])] = slug
        meta = d1_execute(
            """
            INSERT OR IGNORE INTO novels
                (id, title, author, description, language,
                 total_chapters, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                slug, novel.get("title",""), novel.get("author",""),
                novel.get("description",""), "en", 0,
                str(novel.get("created_at","")), str(novel.get("updated_at","")),
            ],
        )
        status = "inserted" if meta.get("rows_written",0) > 0 else "exists  "
        print(f"  [{status}] {novel['title'][:50]}  →  {slug}")
    return id_to_slug

# ── Step 2: Migrate Chapters (parallel) ──────────────────────────────────────

def migrate_chapters(sb: Client, id_to_slug: dict[str, str]) -> None:
    print("\n─────────────────────────────────────────────")
    print(f"  📄 Migrating chapters → R2 + D1  [{MAX_WORKERS} threads]")
    print("─────────────────────────────────────────────\n")

    # Fetch all chapter records first (no content yet — just metadata for batching)
    # Then fetch content in pages and submit to pool
    t_start = time.time()
    total_done = 0
    total_skip = 0
    lock = threading.Lock()

    offset = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        while True:
            batch = (
                sb.table("chapters")
                .select("id, novel_id, chapter_number, chapter_title, content, word_count")
                .range(offset, offset + PAGE_SIZE - 1)
                .order("novel_id").order("chapter_number")
                .execute()
                .data
            )
            if not batch:
                break

            futures = {pool.submit(migrate_chapter, ch, id_to_slug): ch for ch in batch}
            for fut in as_completed(futures):
                try:
                    was_new, label = fut.result()
                    with lock:
                        if was_new:
                            total_done += 1
                        else:
                            total_skip += 1
                        done_so_far = total_done + total_skip
                        elapsed = time.time() - t_start
                        rate = done_so_far / elapsed if elapsed > 0 else 0
                        if was_new:
                            print(
                                f"  ✅ {label:<45}  "
                                f"[{done_so_far:>5} processed | {rate:.1f}/s]"
                            )
                except Exception as e:
                    ch = futures[fut]
                    print(f"  ❌ novel={ch['novel_id']} ch={ch['chapter_number']}: {e}")

            offset += PAGE_SIZE
            print(
                f"  — batch done, offset={offset}  "
                f"total={total_done+total_skip}  "
                f"skipped={total_skip} —"
            )

    print(f"\n  Chapters migrated : {total_done}")
    print(f"  Chapters skipped  : {total_skip} (already existed)")

# ── Step 3: Update total_chapters on each novel ───────────────────────────────

def update_novel_counts() -> None:
    print("\n─────────────────────────────────────────────")
    print("  🔢 Updating total_chapters counts in D1")
    print("─────────────────────────────────────────────\n")
    meta = d1_execute(
        """
        UPDATE novels
        SET total_chapters = (
            SELECT COUNT(*) FROM chapters WHERE chapters.novel_id = novels.id
        )
        """
    )
    print(f"  Updated {meta.get('rows_written','?')} novels")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    print("\n" + "═" * 60)
    print("   🚀  Supabase → Cloudflare D1 + R2  Migration")
    print(f"   Workers: {MAX_WORKERS}  |  Page size: {PAGE_SIZE}")
    print("═" * 60)

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    id_to_slug = migrate_novels(sb)
    migrate_chapters(sb, id_to_slug)
    update_novel_counts()

    elapsed = time.time() - t0
    m, s = divmod(int(elapsed), 60)
    print("\n" + "═" * 60)
    print(f"   🎉  Migration complete in {m}m {s}s")
    print("═" * 60)


if __name__ == "__main__":
    main()
