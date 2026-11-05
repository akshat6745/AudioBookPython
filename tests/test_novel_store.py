"""Tests for app.services.novel_store.store_novel and per-user novel isolation / deletion.

D1 is an in-memory fake and the R2 upload/delete is stubbed, so nothing leaves
the process. What is under test:
  - ownership/visibility of the inserted novel
  - two users uploading the same novel get distinct slugs and see their own copy
  - deleting a novel removes only the caller's uploaded copy (D1 + R2) and
    never touches another user's copy or a public novel.

Run: python -m pytest tests/test_novel_store.py
"""

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

try:  # pragma: no cover
    import app.services.cloudflare_service  # noqa: F401
except Exception:  # pragma: no cover
    stub = types.ModuleType("app.services.cloudflare_service")
    stub.get_chapter_paragraphs = lambda *a, **k: ()
    stub.get_chapter_paragraphs.cache_clear = lambda: None
    stub.upload_chapter_text_to_r2 = lambda *a, **k: ""
    stub._r2_client = lambda: None
    stub._r2_bucket = lambda: ""
    sys.modules["app.services.cloudflare_service"] = stub

from app.api import novels as novels_api  # noqa: E402
from app.api.deps import AuthenticatedUser  # noqa: E402
from app.services import novel_store  # noqa: E402


def _run(coro):
    """Await a coroutine (no pytest-asyncio in this repo)."""
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeD1:
    """In-memory D1 fake supporting users, novels, chapters, and user_progress."""

    def __init__(self, novels=None, users=None):
        self.novels = {n["id"]: dict(n) for n in (novels or [])}
        self.users = list(users or [
            {"id": "u1", "username": "alice"},
            {"id": "u2", "username": "bob"},
        ])
        self.chapters = []
        self.progress = []

    async def query(self, sql, params=None):
        params = params or []
        if "FROM novels n" in sql and "LEFT JOIN user_progress up" in sql:
            uname = str(params[0]).lower()
            uid = next((u["id"] for u in self.users if u["username"].lower() == uname), None)
            out = []
            for n in self.novels.values():
                if n.get("user_id") == uid or n.get("is_public") == 1:
                    out.append({**n, "slug": n["id"], "last_read_date": None})
            return out

        if "SELECT id FROM users WHERE username = ?" in sql:
            uname = str(params[0]).lower()
            return [{"id": u["id"]} for u in self.users if u["username"].lower() == uname]

        if "WHERE user_id = ? AND (id = ? OR LOWER(title) = LOWER(?))" in sql:
            uid, slug, title = params
            return [
                dict(n)
                for n in self.novels.values()
                if n.get("user_id") == uid
                and (n["id"] == slug or (n.get("title") or "").lower() == str(title).lower())
            ]

        if "WHERE id = ? AND user_id = ?" in sql:
            slug, uid = params
            row = self.novels.get(slug)
            if row and row.get("user_id") == uid:
                return [dict(row)]
            return []

        if "FROM novels WHERE user_id = ?" in sql:
            uid = params[0]
            return [dict(n) for n in self.novels.values() if n.get("user_id") == uid]

        if "WHERE (id = ? OR LOWER(title) = LOWER(?)) AND is_public = 1" in sql:
            slug, title = params
            return [
                dict(n)
                for n in self.novels.values()
                if n.get("is_public") == 1
                and (n["id"] == slug or (n.get("title") or "").lower() == str(title).lower())
            ]

        if "WHERE id = ? AND is_public = 1" in sql:
            row = self.novels.get(params[0])
            return [dict(row)] if row and row.get("is_public") == 1 else []

        if "WHERE LOWER(title) = LOWER(?) AND is_public = 1" in sql:
            title = str(params[0]).lower()
            return [
                dict(n)
                for n in self.novels.values()
                if n.get("is_public") == 1 and (n.get("title") or "").lower() == title
            ]

        if "FROM novels WHERE id = ?" in sql:
            row = self.novels.get(params[0])
            return [dict(row)] if row else []

        if "FROM novels WHERE LOWER(title) = LOWER(?)" in sql:
            title = str(params[0]).lower()
            return [dict(n) for n in self.novels.values() if (n.get("title") or "").lower() == title]

        if "SELECT r2_content_path, r2_audio_path FROM chapters WHERE novel_id = ?" in sql:
            nid = params[0]
            return [
                {"r2_content_path": c[4], "r2_audio_path": None}
                for c in self.chapters
                if c[1] == nid
            ]

        if "SELECT 1 FROM chapters WHERE r2_content_path = ?" in sql:
            key = params[0]
            return [{"1": 1}] if any(c[4] == key for c in self.chapters) else []

        if "SELECT 1 FROM chapters WHERE r2_audio_path = ?" in sql:
            return []

        if "SELECT 1 FROM novels WHERE cover_url = ?" in sql:
            key = params[0]
            return [{"1": 1}] if any(n.get("cover_url") == key for n in self.novels.values()) else []

        raise AssertionError(f"unexpected query: {sql}")

    async def execute(self, sql, params=None):
        params = params or []
        if "INTO novels" in sql:
            slug, title, author, total, user_id, is_public = params
            self.novels[slug] = {
                "id": slug,
                "title": title,
                "author": author,
                "total_chapters": total,
                "user_id": user_id,
                "is_public": is_public,
            }
        elif "INTO chapters" in sql:
            self.chapters.append(params)
        elif sql.startswith("UPDATE novels SET cover_url"):
            self.novels[params[1]]["cover_url"] = params[0]
        elif sql.startswith("DELETE FROM user_progress WHERE novel_id = ?"):
            nid = params[0]
            self.progress = [p for p in self.progress if p.get("novel_id") != nid]
        elif sql.startswith("DELETE FROM chapters WHERE novel_id = ?"):
            nid = params[0]
            self.chapters = [c for c in self.chapters if c[1] != nid]
        elif sql.startswith("DELETE FROM novels WHERE id = ? AND user_id = ?"):
            slug, uid = params
            row = self.novels.get(slug)
            if row and row.get("user_id") == uid:
                del self.novels[slug]
                return {"changes": 1}
            return {"changes": 0}
        else:
            raise AssertionError(f"unexpected execute: {sql}")
        return {"changes": 1}


NOVEL = SimpleNamespace(title="Pride and Prejudice", author="Jane Austen")
CHAPTERS = [
    {"chapterNumber": 1, "chapterTitle": "Chapter 1", "content": ["It is a truth.", "Second line."]},
    {"chapterNumber": 2, "chapterTitle": "Chapter 2", "content": ["Another chapter."]},
]


def _patch(monkeypatch, d1, deleted_r2=None):
    monkeypatch.setattr(novel_store, "get_d1_client", lambda: d1)
    monkeypatch.setattr(novels_api, "get_d1_client", lambda: d1)
    monkeypatch.setattr(
        novel_store,
        "upload_chapter_text_to_r2",
        lambda slug, num, text: f"novels/{slug}/{num}.txt.gz",
    )
    if deleted_r2 is not None:
        class FakeS3:
            def delete_object(self, Bucket, Key):
                deleted_r2.append(Key)

        monkeypatch.setattr(novels_api, "_r2_client", lambda: FakeS3())
        monkeypatch.setattr(novels_api, "_r2_bucket", lambda: "test-bucket")


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
    existing = {
        "id": "pride-and-prejudice",
        "title": "Pride and Prejudice",
        "total_chapters": 61,
        "user_id": "someone",
        "is_public": 0,
    }
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


def test_two_users_uploading_same_novel_get_isolated_versions_and_delete_only_their_own(monkeypatch):
    deleted_r2 = []
    d1 = FakeD1()
    _patch(monkeypatch, d1, deleted_r2=deleted_r2)

    # 1. Alice (u1) and Bob (u2) both upload "Pride and Prejudice"
    slug_alice = _run(novel_store.store_novel(NOVEL, CHAPTERS, None, user_id="u1", is_public=False))
    slug_bob = _run(novel_store.store_novel(NOVEL, CHAPTERS, None, user_id="u2", is_public=False))

    assert slug_alice == "pride-and-prejudice"
    assert slug_bob == "pride-and-prejudice-1"

    # Re-uploading by the same user returns None (already in their library)
    assert _run(novel_store.store_novel(NOVEL, CHAPTERS, None, user_id="u1", is_public=False)) is None
    assert _run(novel_store.store_novel(NOVEL, CHAPTERS, None, user_id="u2", is_public=False)) is None

    # 2. Each user sees only their own uploaded version in GET /novels
    alice_list = _run(novels_api.fetch_names(caller=AuthenticatedUser(id="u1", username="alice")))
    bob_list = _run(novels_api.fetch_names(caller=AuthenticatedUser(id="u2", username="bob")))

    assert [n.slug for n in alice_list] == ["pride-and-prejudice"]
    assert alice_list[0].isOwner is True
    assert [n.slug for n in bob_list] == ["pride-and-prejudice-1"]
    assert bob_list[0].isOwner is True

    # 3. Resolving by title or base slug returns each user's own uploaded version
    assert _run(novels_api.resolve_novel_id(d1, "Pride and Prejudice", username="alice")) == "pride-and-prejudice"
    assert _run(novels_api.resolve_novel_id(d1, "Pride and Prejudice", username="bob")) == "pride-and-prejudice-1"
    assert _run(novels_api.resolve_novel_id(d1, "pride-and-prejudice", username="bob")) == "pride-and-prejudice-1"

    # 4. Bob cannot delete Alice's novel by Alice's slug if Bob doesn't own it,
    #    and when Bob deletes his own novel, only Bob's copy and R2 files are deleted.
    res = _run(
        novels_api.delete_novel(
            "pride-and-prejudice-1",
            caller=AuthenticatedUser(id="u2", username="bob"),
        )
    )
    assert res["deleted"] is True
    assert res["novelId"] == "pride-and-prejudice-1"
    assert "pride-and-prejudice-1" not in d1.novels
    assert "pride-and-prejudice" in d1.novels
    assert any(k.startswith("novels/pride-and-prejudice-1/") for k in deleted_r2)
    assert not any(k.startswith("novels/pride-and-prejudice/") for k in deleted_r2)

    # 5. Now that Bob deleted his copy, if Bob tries to delete Alice's slug, it 404s
    with pytest.raises(HTTPException) as exc:
        _run(
            novels_api.delete_novel(
                "pride-and-prejudice",
                caller=AuthenticatedUser(id="u2", username="bob"),
            )
        )
    assert exc.value.status_code == 404
    assert "pride-and-prejudice" in d1.novels


def test_cannot_delete_public_catalog_novel(monkeypatch):
    d1 = FakeD1()
    _patch(monkeypatch, d1, deleted_r2=[])
    _run(novel_store.store_novel(NOVEL, CHAPTERS, None, user_id=None, is_public=True))

    with pytest.raises(HTTPException) as exc:
        _run(
            novels_api.delete_novel(
                "pride-and-prejudice",
                caller=AuthenticatedUser(id="u1", username="alice"),
            )
        )
    assert exc.value.status_code == 403
    assert "pride-and-prejudice" in d1.novels
