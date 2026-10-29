"""Read-only validation of source, tag, and audio manifests. Does not import models."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from .exitcodes import EXIT_INCOMPLETE, EXIT_INVALID, EXIT_OK, code_for_statuses
from .hashing import sha256_bytes
from .layout import (
    AUDIO_SCHEMA,
    SOURCE_SCHEMA,
    STYLETTS_RUNTIME,
    STYLETTS_VERSION,
    TAG_SCHEMA,
    TAGGER_MODEL,
    TAGGER_RUNTIME,
    TAGGER_VERSION,
    VERIFIED_CHECKPOINT,
    audio_prefix,
    source_manifest_key,
    tag_manifest_key,
)
from .leases import lease_view
from .store import NotFound

VALID_TAG = frozenset({"valid"})
AUDIO_READY = frozenset({"valid", "legacy_valid"})


def _json(store, key: str) -> tuple[dict | None, bytes | None, str]:
    head = store.head(key)
    if head is None:
        return None, None, "missing"
    try:
        body = store.get_bytes(key)
    except NotFound:
        return None, None, "missing"
    if len(body) != head.size:
        return None, body, "hash_mismatch"
    try:
        return json.loads(body.decode()), body, "ok"
    except json.JSONDecodeError:
        return None, body, "invalid"


def load_source(store, slug: str) -> dict:
    payload, _body, state = _json(store, source_manifest_key(slug))
    if state != "ok" or not payload:
        raise FileNotFoundError(f"source manifest missing for {slug}")
    if payload.get("schema") != SOURCE_SCHEMA or payload.get("slug") != slug:
        raise ValueError(f"source manifest for {slug} is invalid")
    return payload


def source_chapter(source: dict, chapter: int) -> dict | None:
    for row in source.get("chapters") or []:
        if int(row["chapter"]) == int(chapter):
            return row
    return None


def _object_matches(store, key: str, expected_size: int, expected_sha: str | None) -> str:
    head = store.head(key)
    if head is None:
        return "missing"
    compressed = key.endswith(".gz") or key.startswith("novels/")
    if not compressed and head.size != int(expected_size):
        return "hash_mismatch"
    if expected_sha or compressed:
        body = store.get_bytes(key)
        if len(body) != int(expected_size) or (expected_sha and sha256_bytes(body) != expected_sha):
            return "hash_mismatch"
    return "ok"


def classify_tag(store, source: dict, chapter: int, *, now: float) -> dict:
    slug = source["slug"]
    spec = source_chapter(source, chapter)
    row = {"chapter": chapter, "status": "invalid", "detail": ""}
    if spec is None:
        row["status"] = "invalid"
        row["detail"] = "chapter is not in the source manifest"
        return row
    payload, body, state = _json(store, tag_manifest_key(slug, chapter))
    if state == "missing":
        lease = lease_view(store, slug, "tag", chapter, now)
        if lease and not lease.get("stale"):
            row["status"] = "leased"
            row["detail"] = f"tag lease held by {lease.get('host')}/{lease.get('run_id')}/{lease.get('worker_index')}"
            return row
        row["status"] = "missing"
        return row
    if state != "ok" or payload is None or body is None:
        row["status"] = "invalid"
        row["detail"] = "tag manifest is not JSON"
        return row
    reasons = []
    if payload.get("schema") != TAG_SCHEMA:
        reasons.append("schema")
    if payload.get("tagger_version") != TAGGER_MODEL or payload.get("runtime_version") != TAGGER_VERSION:
        reasons.append("model")
    if payload.get("tagger_runtime") not in {None, TAGGER_RUNTIME} and payload.get("tagger_runtime") != TAGGER_RUNTIME:
        reasons.append("runtime")
    if payload.get("source_sha256") != spec.get("sha256"):
        reasons.append("source")
    if int(payload.get("paragraph_count") or -1) != int(spec.get("paragraphs") or -2):
        reasons.append("paragraph_count")
    if reasons:
        row["status"] = "stale"
        row["detail"] = "tag dependency mismatch: " + ",".join(reasons)
        row["manifest_sha256"] = sha256_bytes(body)
        return row
    blob_key = payload.get("jsonl_key")
    blob_state = _object_matches(store, blob_key, int(payload.get("jsonl_bytes") or -1), payload.get("jsonl_sha256"))
    if blob_state == "missing":
        row["status"] = "conflict"
        row["detail"] = "tag manifest has no payload object"
        return row
    if blob_state != "ok":
        row["status"] = "hash_mismatch"
        row["detail"] = "tag JSONL hash or size does not match the manifest"
        return row
    # Count lines without loading a model.
    text = store.get_bytes(blob_key).decode()
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != int(payload["paragraph_count"]):
        row["status"] = "invalid"
        row["detail"] = f"tag line count {len(lines)} != paragraph_count {payload['paragraph_count']}"
        return row
    try:
        for line in lines:
            item = json.loads(line)
            for field in ("emotion", "style", "sfx", "confidence", "style_conf", "sfx_conf"):
                if field not in item:
                    raise ValueError(field)
    except (json.JSONDecodeError, ValueError) as exc:
        row["status"] = "invalid"
        row["detail"] = f"tag schema field missing: {exc}"
        return row
    row["status"] = "valid"
    row["manifest_sha256"] = sha256_bytes(body)
    row["jsonl_sha256"] = payload.get("jsonl_sha256")
    return row


def _audio_entries(manifest: dict) -> list[dict]:
    entries = []
    if manifest.get("title"):
        entries.append(manifest["title"])
    entries.extend(manifest.get("paragraphs") or [])
    if manifest.get("chapter_file"):
        entries.append(manifest["chapter_file"])
    return entries


def _audio_fingerprint_reasons(manifest: dict, spec: dict, tag_row: dict | None) -> list[str]:
    reasons = []
    if manifest.get("schema") != AUDIO_SCHEMA:
        reasons.append("schema")
    if manifest.get("source_sha256") != spec.get("sha256"):
        reasons.append("source")
    if manifest.get("runtime") not in {None, STYLETTS_RUNTIME} and manifest.get("runtime") != STYLETTS_RUNTIME:
        reasons.append("runtime")
    if manifest.get("runtime_version") != STYLETTS_VERSION:
        reasons.append("runtime_version")
    if manifest.get("checkpoint_sha256") != VERIFIED_CHECKPOINT["sha256"]:
        reasons.append("checkpoint")
    if tag_row and tag_row.get("status") == "valid":
        if manifest.get("tag_manifest_sha256") != tag_row.get("manifest_sha256"):
            reasons.append("tag")
        if manifest.get("tagger_version") != TAGGER_MODEL:
            reasons.append("tagger")
    return reasons


def classify_audio(store, source: dict, chapter: int, *, now: float, tag_row: dict | None = None) -> dict:
    slug = source["slug"]
    spec = source_chapter(source, chapter)
    row = {"chapter": chapter, "status": "invalid", "detail": "", "prefix": audio_prefix(slug, chapter)}
    if spec is None:
        row["detail"] = "chapter is not in the source manifest"
        return row
    prefix = audio_prefix(slug, chapter)
    manifest_key = prefix + "manifest.json"
    payload, body, state = _json(store, manifest_key)
    objects = [item for item in store.list(prefix) if item.key != manifest_key and not item.key.endswith("/")]
    # Ignore nested versioned prefixes when listing the canonical chapter prefix.
    versioned_marker = prefix + "runtimes/"
    objects = [item for item in objects if not item.key.startswith(versioned_marker)]
    if state == "missing":
        publish = store.head(prefix + "publish.json")
        if publish is not None:
            row["status"] = "publishing"
            row["detail"] = "audio publish elected but manifest is not complete"
            return row
        lease = lease_view(store, slug, "audio", chapter, now)
        if lease and not lease.get("stale") and not objects:
            row["status"] = "leased"
            row["detail"] = f"audio lease held by {lease.get('host')}/{lease.get('run_id')}/{lease.get('worker_index')}"
            return row
        if objects:
            row["status"] = "conflict"
            row["detail"] = f"{len(objects)} audio objects exist without a manifest; refusing to overwrite"
            return row
        if lease and not lease.get("stale"):
            row["status"] = "leased"
            row["detail"] = "audio lease held"
            return row
        row["status"] = "missing"
        return row
    if state != "ok" or payload is None or body is None:
        row["status"] = "invalid"
        row["detail"] = "audio manifest is not JSON"
        return row
    legacy = payload.get("schema") in {None, "styletts2-eye-v1/legacy"}
    if not legacy:
        reasons = _audio_fingerprint_reasons(payload, spec, tag_row)
        if reasons:
            row["status"] = "stale"
            row["detail"] = "audio dependency mismatch: " + ",".join(reasons)
            row["manifest_sha256"] = sha256_bytes(body)
            return row
    entries = _audio_entries(payload)
    if not entries:
        row["status"] = "invalid"
        row["detail"] = "audio manifest lists no files"
        return row
    # Fast path for BotoStore: check all listed objects in parallel against sizes first.
    if store.__class__.__name__ == "BotoStore":
        by_key = {item.key: item for item in objects}
        for entry in entries:
            name = entry.get("file")
            if not name or "/" in name or name.startswith("."):
                row["status"] = "invalid"
                row["detail"] = "audio manifest file name is unsafe"
                return row
            head = by_key.get(prefix + name)
            if head is None:
                row["status"] = "conflict"
                row["detail"] = f"manifest references missing {name}"
                return row
            if head.size != int(entry.get("bytes") or -1):
                row["status"] = "hash_mismatch"
                row["detail"] = f"{name} size or sha256 does not match the manifest"
                return row
        row["status"] = "legacy_valid" if legacy else "valid"
        row["manifest_sha256"] = sha256_bytes(body)
        return row
    for entry in entries:
        name = entry.get("file")
        if not name or "/" in name or name.startswith("."):
            row["status"] = "invalid"
            row["detail"] = "audio manifest file name is unsafe"
            return row
        state = _object_matches(store, prefix + name, int(entry.get("bytes") or -1), entry.get("sha256"))
        if state == "missing":
            row["status"] = "conflict"
            row["detail"] = f"manifest references missing {name}"
            return row
        if state != "ok":
            row["status"] = "hash_mismatch"
            row["detail"] = f"{name} size or sha256 does not match the manifest"
            return row
    row["status"] = "legacy_valid" if legacy else "valid"
    row["manifest_sha256"] = sha256_bytes(body)
    return row


def classify_source_chapter(store, source: dict, chapter: int) -> dict:
    spec = source_chapter(source, chapter)
    row = {"chapter": chapter, "status": "invalid", "detail": ""}
    if spec is None:
        row["detail"] = "not in source manifest"
        return row
    state = _object_matches(store, spec["key"], int(spec["bytes"]), spec["sha256"])
    if state == "ok":
        row["status"] = "valid"
        return row
    if state == "missing":
        row["status"] = "missing"
        return row
    row["status"] = "hash_mismatch"
    row["detail"] = "source chapter bytes do not match the manifest"
    return row


def _pool_map(workers: int, fn, items: list):
    count = max(1, workers)
    if count == 1 or len(items) <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(fn, items))


def validate_stage(store, source: dict, stage: str, chapters: list[int], *, now: float, workers: int = 1) -> list[dict]:
    slug_chapters = chapters

    def one_source(chapter: int) -> dict:
        return classify_source_chapter(store, source, chapter)

    def one_tag(chapter: int) -> dict:
        return classify_tag(store, source, chapter, now=now)

    def one_audio(chapter: int) -> dict:
        tag_row = classify_tag(store, source, chapter, now=now)
        audio_row = classify_audio(store, source, chapter, now=now, tag_row=tag_row)
        audio_row["tag_status"] = tag_row["status"]
        return audio_row

    if stage == "source":
        return _pool_map(workers, one_source, slug_chapters)
    if stage == "tag":
        return _pool_map(workers, one_tag, slug_chapters)
    if stage == "audio":
        return _pool_map(workers, one_audio, slug_chapters)
    if stage == "all":
        rows = []
        for label, fn in (("source", one_source), ("tag", one_tag), ("audio", one_audio)):
            for row in _pool_map(workers, fn, slug_chapters):
                rows.append({**row, "part": label})
        return rows
    raise ValueError(f"unknown validation stage {stage}")


def worst_code(rows: list[dict]) -> int:
    return code_for_statuses([row.get("status", "invalid") for row in rows]) if rows else EXIT_OK


def tag_block_reason(rows: list[dict]) -> str | None:
    bad = [row for row in rows if row.get("status") not in VALID_TAG]
    if not bad:
        return None
    return _summarize("tag", bad)


def _summarize(stage: str, rows: list[dict]) -> str:
    grouped: dict[str, list[int]] = {}
    for row in rows:
        grouped.setdefault(row.get("status", "invalid"), []).append(int(row["chapter"]))
    parts = [f"{status} {chapters}" for status, chapters in sorted(grouped.items())]
    code = EXIT_INVALID if any(row["status"] in {"invalid", "stale", "hash_mismatch", "conflict"} for row in rows) else EXIT_INCOMPLETE
    return f"{stage} preflight failed (exit {code}): " + "; ".join(parts)
