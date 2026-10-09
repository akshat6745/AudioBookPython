"""Import 20 well-known public-domain novels from Project Gutenberg as public
novels that every user sees in their library.

Runs entirely on this machine: it downloads each EPUB, parses it with the
backend's own parser, and writes straight to D1 + R2 through the same
`store_novel` the /upload-epub endpoint uses. The production server is never
called, so a bulk import puts no load on it — and no login or JWT is needed.
The novels are stored unowned (user_id NULL) and public (is_public = 1).

Usage (PYTHONPATH=. so `app` resolves when run as a script):
    PYTHONPATH=. python scripts/upload_public_domain_novels.py --dry-run   # download + parse only
    PYTHONPATH=. python scripts/upload_public_domain_novels.py             # import

Requires a full local .env (Cloudflare D1 + R2 credentials; SUPABASE_URL /
SUPABASE_KEY are also required fields in app.core.settings).

Uses Gutenberg's no-images EPUBs: the upload path still stores EPUB images in
Supabase, which is being retired, and text-only books are all this needs.
Re-running is safe — downloads are cached in downloads/gutenberg/, and novels
that already exist in D1 are skipped.
"""

import argparse
import asyncio
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from app.services.epub_parser import parse_epub_content  # noqa: E402

# Gutenberg ebook ID → title. Public domain in the US; Gutenberg asks bulk
# downloaders to identify themselves and go slowly, hence the delay below.
NOVELS = {
    1342: "Pride and Prejudice",
    84: "Frankenstein",
    11: "Alice's Adventures in Wonderland",
    1661: "The Adventures of Sherlock Holmes",
    2701: "Moby Dick",
    98: "A Tale of Two Cities",
    345: "Dracula",
    1260: "Jane Eyre",
    768: "Wuthering Heights",
    174: "The Picture of Dorian Gray",
    2554: "Crime and Punishment",
    1400: "Great Expectations",
    64317: "The Great Gatsby",
    120: "Treasure Island",
    76: "Adventures of Huckleberry Finn",
    35: "The Time Machine",
    36: "The War of the Worlds",
    1184: "The Count of Monte Cristo",
    103: "Around the World in Eighty Days",
    164: "Twenty Thousand Leagues under the Sea",
}

GUTENBERG_EPUB_URL = "https://www.gutenberg.org/cache/epub/{id}/pg{id}.epub"
USER_AGENT = "AudioBookPython-uploader/1.0 (personal audiobook library)"
MAX_EPUB_BYTES = 20 * 1024 * 1024
DOWNLOAD_DELAY_SECONDS = 2

CACHE_DIR = Path(__file__).resolve().parent.parent / "downloads" / "gutenberg"


def is_valid_epub(data: bytes) -> bool:
    """A real EPUB is a zip whose `mimetype` entry is application/epub+zip."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            return zf.read("mimetype").strip() == b"application/epub+zip"
    except (zipfile.BadZipFile, KeyError):
        return False


def download_epub(session: requests.Session, book_id: int) -> Path:
    """Download one EPUB into the cache (or reuse it) and return its path."""
    # The filename comes from an int ID in NOVELS, never from the network.
    path = CACHE_DIR / f"pg{int(book_id)}.epub"
    if path.exists() and is_valid_epub(path.read_bytes()):
        return path

    # Fixed Gutenberg URL; redirects are refused so the request can't be
    # bounced to another host.
    resp = session.get(
        GUTENBERG_EPUB_URL.format(id=int(book_id)),
        timeout=60,
        allow_redirects=False,
        stream=True,
    )
    resp.raise_for_status()

    data = bytearray()
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        data.extend(chunk)
        if len(data) > MAX_EPUB_BYTES:
            raise ValueError(f"larger than {MAX_EPUB_BYTES // (1024 * 1024)} MB")
    if not is_valid_epub(bytes(data)):
        raise ValueError("response is not a valid EPUB")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    time.sleep(DOWNLOAD_DELAY_SECONDS)
    return path


async def run(dry_run: bool) -> int:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    if not dry_run:
        # Imported here so --dry-run works without Cloudflare credentials.
        from app.core.d1_client import get_d1_client
        from app.services.novel_store import store_novel

    imported, skipped, failed = [], [], []
    loop = asyncio.get_running_loop()
    for i, (book_id, title) in enumerate(NOVELS.items(), start=1):
        prefix = f"[{i:2}/{len(NOVELS)}] {title}"
        try:
            path = download_epub(session, book_id)
            # Parsing is synchronous and CPU-bound — keep it off the event loop.
            novel, chapters, _images, cover_image = await loop.run_in_executor(
                None, parse_epub_content, path.read_bytes()
            )
            if dry_run:
                print(f"{prefix}: parsed {len(chapters)} chapters")
                imported.append(title)
                continue

            started = time.monotonic()
            slug = await store_novel(
                novel, chapters, cover_image, user_id=None, is_public=True
            )
            if slug is None:
                print(f"{prefix}: already exists — skipped")
                skipped.append(title)
            else:
                print(
                    f"{prefix}: imported as '{slug}', {len(chapters)} chapters, public "
                    f"({time.monotonic() - started:.0f}s)"
                )
                imported.append(title)
        except Exception as exc:  # report and carry on with the next book
            print(f"{prefix}: FAILED — {type(exc).__name__}: {exc}")
            failed.append(title)

    if not dry_run:
        await get_d1_client().close()

    verb = "parsed" if dry_run else "imported"
    print(f"\n{verb}: {len(imported)}   skipped (already exist): {len(skipped)}   failed: {len(failed)}")
    if skipped:
        print("Skipped novels keep their current owner and visibility.")
    if failed:
        print("failed:", ", ".join(failed))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="download and parse only; write nothing"
    )
    args = parser.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
