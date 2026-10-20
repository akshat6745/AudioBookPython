"""Emotion tagging stage. Importing this module does not import StyleTTS or the tagger weights."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import layout
from .batching import call_reducing, resolve_batch
from .exitcodes import EXIT_INVALID, EXIT_OK, merge_codes
from .hashing import sha256_bytes
from .leases import claim_chapter
from .profile import load_profile, save_effective
from .store import ConflictError
from .textutil import sha256_text, tag_rows_for_chapter
from .validate import classify_tag, load_source

REDO = frozenset({"missing", "stale"})
BLOCKED = frozenset({"conflict", "hash_mismatch", "invalid"})


def _load_tagger():
    from . import tagger_model

    return tagger_model


def _model_key() -> str:
    return f"{layout.TAGGER_RUNTIME}@{layout.TAGGER_VERSION}"


def tag_manifest_body(*, source_chapter: dict, paragraph_count: int, digest: str, size: int, blob_key: str, batch_requested: int, batch_effective: int, worker_id: str) -> dict:
    return {
        "schema": layout.TAG_SCHEMA,
        "novel": source_chapter.get("slug"),
        "chapter": int(source_chapter["chapter"]),
        "source_sha256": source_chapter["sha256"],
        "source_bytes": int(source_chapter["bytes"]),
        "tagger_runtime": layout.TAGGER_RUNTIME,
        "tagger_version": layout.TAGGER_MODEL,
        "runtime_version": layout.TAGGER_VERSION,
        "paragraph_count": paragraph_count,
        "jsonl_key": blob_key,
        "jsonl_sha256": digest,
        "jsonl_bytes": size,
        "batch_requested": batch_requested,
        "batch_effective": batch_effective,
        "worker_id": worker_id,
    }


def publish_tag(store, slug: str, chapter: int, payload: bytes, manifest: dict) -> str:
    """Upload the JSONL blob first and the manifest last. Never replace a blob that differs."""
    from .layout import assert_publish_key_allowed, tag_blob_key, tag_manifest_key

    digest = sha256_bytes(payload)
    blob = tag_blob_key(slug, digest)
    man_key = tag_manifest_key(slug, chapter)
    assert_publish_key_allowed(blob)
    assert_publish_key_allowed(man_key)
    manifest = dict(manifest)
    manifest["jsonl_key"] = blob
    manifest["jsonl_sha256"] = digest
    manifest["jsonl_bytes"] = len(payload)
    existing_head = store.head(blob)
    if existing_head is None:
        store.put_bytes(blob, payload, content_type="application/x-ndjson", if_none_match="*")
    elif existing_head.size != len(payload) or sha256_bytes(store.get_bytes(blob)) != digest:
        raise ConflictError(f"tag blob size conflict for chapter {chapter}")
    body = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode()
    head = store.head(man_key)
    if head is None:
        store.put_bytes(man_key, body, content_type="application/json", if_none_match="*")
    else:
        current = json.loads(store.get_bytes(man_key).decode())
        same = (
            current.get("jsonl_sha256") == digest
            and current.get("source_sha256") == manifest["source_sha256"]
            and current.get("schema") == layout.TAG_SCHEMA
            and current.get("tagger_version") == layout.TAGGER_MODEL
        )
        if not same:
            raise ConflictError(f"tag manifest for chapter {chapter} already differs; refusing to replace it")
        return "exists"
    remote = store.get_bytes(blob)
    if sha256_bytes(remote) != digest or len(remote) != len(payload):
        raise ConflictError(f"tag blob verification failed for chapter {chapter}")
    written = store.get_bytes(man_key)
    if sha256_bytes(written) != sha256_bytes(body):
        raise ConflictError(f"tag manifest verification failed for chapter {chapter}")
    return "published"


def chapters_to_tag(store, source: dict, chapters: list[int], now: float) -> tuple[list[int], list[dict]]:
    rows = [classify_tag(store, source, chapter, now=now) for chapter in chapters]
    work = [int(row["chapter"]) for row in rows if row["status"] in REDO or row["status"] == "leased"]
    return work, rows


def process_chapter(store, source: dict, chapter: int, *, batch: int, device: str, worker_id: str, tagger, profile_file: Path | None, log) -> tuple[int, int]:
    spec = next(item for item in source["chapters"] if int(item["chapter"]) == chapter)
    text = store.get_bytes(spec["key"]).decode()
    if sha256_text(text) != spec["sha256"]:
        raise ConflictError(f"source chapter {chapter} hash changed")
    rows = tag_rows_for_chapter(text)
    if len(rows) != int(spec["paragraphs"]):
        raise ConflictError(f"chapter {chapter} paragraph count does not match the source manifest")

    def attempt(width: int):
        return tagger.tag_chapter(rows, batch_size=width, device=device)

    def on_reduce(width, _exc):
        log(f"chapter {chapter} tag batch reduced to {width} after OOM")
        from .deviceutil import empty_cache

        empty_cache(device)

    tagged, effective, _tried = call_reducing(batch, attempt, on_reduce=on_reduce)
    if len(tagged) != len(rows):
        raise ConflictError(f"tagger returned {len(tagged)} rows for {len(rows)} paragraphs")
    lines = []
    for row, label in zip(rows, tagged):
        lines.append(json.dumps({"paragraph": row["paragraph"], **label}, ensure_ascii=False))
    payload = ("\n".join(lines) + "\n").encode()
    manifest = tag_manifest_body(
        source_chapter={**spec, "slug": source["slug"]},
        paragraph_count=len(rows),
        digest="",
        size=0,
        blob_key="",
        batch_requested=batch,
        batch_effective=effective,
        worker_id=worker_id,
    )
    publish_tag(store, source["slug"], chapter, payload, manifest)
    save_effective("tag", device, _model_key(), effective, profile_file)
    log(f"chapter {chapter} tagged paragraphs={len(rows)} batch={effective}")
    return 0, effective


def worker_loop(store, source, chapters, *, identity: dict, batch: int, device: str, now_fn, log, tagger=None, profile_file=None, ttl: float | None = None) -> int:
    """Claim distinct chapters via R2 until none remain claimable."""
    import os
    import time

    owner = identity["owner"]
    slug = source["slug"]
    codes = []
    while True:
        claimed = None
        target = None
        now = now_fn()
        for chapter in chapters:
            row = classify_tag(store, source, chapter, now=now)
            if row["status"] not in REDO and row["status"] != "leased":
                if row["status"] in BLOCKED:
                    codes.append(EXIT_INVALID)
                continue
            kwargs = dict(
                store=store,
                slug=slug,
                stage="tag",
                chapter=chapter,
                owner=owner,
                run_id=identity["run_id"],
                worker_index=identity["worker_index"],
                runtime_version=layout.TAGGER_VERSION,
                now=now,
                host=identity["host"],
                pid=os.getpid(),
            )
            if ttl is not None:
                kwargs["ttl"] = ttl
            claimed = claim_chapter(**kwargs)
            if claimed is None:
                continue
            again = classify_tag(store, source, chapter, now=now_fn())
            # A fresh claim of our own reads as leased. That is still our work.
            # valid/conflict/hash_mismatch means someone else finished or blocked it.
            if again["status"] not in REDO and again["status"] != "leased":
                claimed.release(now_fn())
                claimed = None
                continue
            target = chapter
            break
        if target is None:
            break
        assert claimed is not None
        try:
            from .leases import Heartbeat

            with Heartbeat(claimed, now_fn):
                if tagger is None:
                    tagger = _load_tagger()
                code, _effective = process_chapter(
                    store, source, target, batch=batch, device=device,
                    worker_id=identity["worker_id"], tagger=tagger, profile_file=profile_file, log=log,
                )
            codes.append(code)
        except Exception:
            codes.append(EXIT_INVALID)
            raise
        finally:
            claimed.release(now_fn())
            # keep the heartbeat honest for long chapters: callers may heartbeat explicitly
            _ = time.time
    return merge_codes(*codes) if codes else EXIT_OK
