"""Tests for app.services.novel_store.store_novel.

D1 is an in-memory fake and the R2 upload is stubbed, so nothing leaves the
process. What is under test: ownership/visibility of the inserted novel, the
"already exists" short-circuit, and slug de-duplication.

Run: python -m pytest tests/test_novel_store.py
"""

import asyncio
from types import SimpleNamespace

from app.services import novel_store


def _run(coro):
    """Await a coroutine (no pytest-asyncio in this repo)."""
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeD1:
    """Just enough of the D1 client for store_novel."""

    def __init__(self, novels=None):
        self.novels = {n["id"]: dict(n) for n in (novels or [])}
        self.chapters = []

    async def query(self, sql, params=None):
        if "FROM novels WHERE id = ?" in sql:
            row = self.novels.get(params[0])
            return [dict(row)] if row else []
        raise AssertionError(f"unexpected query: {sql}")

    async def execute(self, sql, params=None):
        if "INTO novels" in sql:
            slug, title, author, total, user_id, is_public = params
            self.novels[slug] = {
                "id": slug, "title": title, "author": author,
                "total_chapters": total, "user_id": user_id, "is_public": is_public,
            }
        elif "INTO chapters" in sql:
            self.chapters.append(params)
        elif sql.startswith("UPDATE novels SET cover_url"):
            self.novels[params[1]]["cover_url"] = params[0]
        else:
            raise AssertionError(f"unexpected execute: {sql}")
        return {"changes": 1}


NOVEL = SimpleNamespace(title="Pride and Prejudice", author="Jane Austen")
CHAPTERS = [
    {"chapterNumber": 1, "chapterTitle": "Chapter 1", "content": ["It is a truth.", "Second line."]},
    {"chapterNumber": 2, "chapterTitle": "Chapter 2", "content": ["Another chapter."]},
]


def _patch(monkeypatch, d1):
    monkeypatch.setattr(novel_store, "get_d1_client", lambda: d1)
    monkeypatch.setattr(
        novel_store, "upload_chapter_text_to_r2",
        lambda slug, num, text: f"novels/{slug}/{num}.txt.gz",
    )


def test_public_unowned_novel_is_stored(monkeypatch):
    d1 = FakeD1()
    _patch(monkeypatch, d1)

    slug = _run(novel_store.store_novel(NOVEL, CHAPTERS, None, user_id=None, is_public=True))

    assert slug == "pride-and-prejudice"
    row = d1.novels[slug]
    assert row["user_id"] is None
    assert row["is_public"] == 1
    assert row["total_chapters"] == 2
    assert sorted(c[2] for c in d1.chapters) == [1, 2]
    assert all(c[1] == slug for c in d1.chapters)


def test_owned_upload_stays_private(monkeypatch):
    d1 = FakeD1()
    _patch(monkeypatch, d1)

    slug = _run(novel_store.store_novel(NOVEL, CHAPTERS, None, user_id="u1", is_public=False))

    assert d1.novels[slug]["user_id"] == "u1"
    assert d1.novels[slug]["is_public"] == 0


def test_existing_novel_is_not_overwritten(monkeypatch):
    existing = {"id": "pride-and-prejudice", "title": "Pride and Prejudice",
                "total_chapters": 61, "user_id": "someone", "is_public": 0}
    d1 = FakeD1([existing])
    _patch(monkeypatch, d1)

    assert _run(novel_store.store_novel(NOVEL, CHAPTERS, None, user_id=None, is_public=True)) is None
    assert d1.novels["pride-and-prejudice"] == existing
    assert d1.chapters == []


def test_empty_slug_collision_gets_suffix(monkeypatch):
    # A row with no chapters (e.g. a failed earlier upload) holds the slug.
    d1 = FakeD1([{"id": "pride-and-prejudice", "title": "Pride and Prejudice", "total_chapters": 0}])
    _patch(monkeypatch, d1)

    slug = _run(novel_store.store_novel(NOVEL, CHAPTERS, None, user_id=None, is_public=True))

    assert slug == "pride-and-prejudice-1"
    assert d1.novels[slug]["is_public"] == 1
