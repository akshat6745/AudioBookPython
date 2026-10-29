"""Publish chapter audio without replacing bytes that already differ.

Files go to a unique attempt prefix first. ``publish.json`` is elected with
If-None-Match. Final objects are created with If-None-Match and checked when they
already exist. The manifest is last. A partial canonical chapter is a conflict.
"""

from __future__ import annotations

import json

from .hashing import sha256_bytes
from .layout import (
    AUDIO_SCHEMA,
    STYLETTS_RUNTIME,
    STYLETTS_VERSION,
    TAGGER_MODEL,
    VERIFIED_CHECKPOINT,
    KNOWN_SHA256,
    attempt_prefix,
    audio_prefix,
)
from .store import ConflictError, PreconditionFailed


def destination_for(status: str, slug: str, chapter: int, *, replace_stale: bool = False) -> str:
    if status == "stale" and not replace_stale:
        return audio_prefix(slug, chapter, versioned=True)
    return audio_prefix(slug, chapter)


def _manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode()


def publish_chapter_audio(
    store,
    *,
    slug: str,
    chapter: int,
    dest_prefix: str,
    files: dict[str, bytes],
    manifest: dict,
    attempt_id: str,
    overwrite: bool = False,
) -> str:
    """``files`` maps the final object name (title.opus, 0.opus, chapter.opus) to bytes."""
    if not dest_prefix.endswith("/"):
        raise ValueError("audio prefix must end with /")
    if any(name.startswith("/") or "/" in name for name in files):
        raise ValueError("audio file names must be a single path segment")
    attempt = attempt_prefix(slug, "audio", attempt_id, chapter)
    for name, body in files.items():
        store.put_bytes(attempt + name, body, content_type="audio/ogg", if_none_match="*")
        remote = store.get_bytes(attempt + name)
        if remote != body:
            raise ConflictError(f"attempt upload mismatch for {name}")
    manifest = dict(manifest)
    manifest["schema"] = AUDIO_SCHEMA
    manifest["runtime"] = STYLETTS_RUNTIME
    manifest["runtime_version"] = STYLETTS_VERSION
    manifest["checkpoint_sha256"] = VERIFIED_CHECKPOINT["sha256"]
    manifest["style_bank_sha256"] = KNOWN_SHA256["style_bank/bank.npz"]
    manifest["voice_bank"] = manifest.get("voice_bank") or getattr(__import__("scripts.gpu_tts.student_tts.distributed.layout", fromlist=["VOICE_BANK"]), "VOICE_BANK", "kokoro_af_heart_d018")
    manifest["tagger_version"] = manifest.get("tagger_version") or TAGGER_MODEL
    manifest["prefix"] = dest_prefix.rstrip("/")
    manifest_body = _manifest_bytes(manifest)
    store.put_bytes(attempt + "manifest.json", manifest_body, content_type="application/json", if_none_match="*")
    plan = {
        "attempt_id": attempt_id,
        "attempt_prefix": attempt,
        "files": {name: {"bytes": len(body), "sha256": sha256_bytes(body)} for name, body in files.items()},
        "manifest_sha256": sha256_bytes(manifest_body),
        "manifest_bytes": len(manifest_body),
    }
    plan_key = dest_prefix + "publish.json"
    plan_body = (json.dumps(plan, sort_keys=True) + "\n").encode()
    if overwrite:
        store.put_bytes(plan_key, plan_body, content_type="application/json")
    else:
        try:
            store.put_bytes(plan_key, plan_body, content_type="application/json", if_none_match="*")
        except PreconditionFailed:
            existing = json.loads(store.get_bytes(plan_key).decode())
            if existing.get("files") != plan["files"]:
                raise ConflictError(
                    f"chapter {chapter} already has a different audio publish election; not overwriting"
                )
            plan = existing
            attempt = existing["attempt_prefix"]
    for name, meta in plan["files"].items():
        body = store.get_bytes(attempt + name)
        if len(body) != meta["bytes"] or sha256_bytes(body) != meta["sha256"]:
            raise ConflictError(f"attempt object {name} does not match the publish election")
        final_key = dest_prefix + name
        if overwrite:
            store.put_bytes(final_key, body, content_type="audio/ogg")
            continue
        head = store.head(final_key)
        if head is None:
            try:
                store.put_bytes(final_key, body, content_type="audio/ogg", if_none_match="*")
            except PreconditionFailed:
                head = store.head(final_key)
                if head is None or head.size != len(body):
                    raise ConflictError(f"refusing to overwrite {final_key}") from None
        elif head.size != len(body):
            raise ConflictError(f"refusing to overwrite {final_key}: remote {head.size} bytes, local {len(body)}")
        else:
            remote = store.get_bytes(final_key)
            if sha256_bytes(remote) != meta["sha256"]:
                raise ConflictError(f"refusing to overwrite {final_key}: hash differs")
    body = store.get_bytes(attempt + "manifest.json")
    if sha256_bytes(body) != plan.get("manifest_sha256", sha256_bytes(body)):
        raise ConflictError(f"attempt manifest does not match the publish election for chapter {chapter}")
    man_key = dest_prefix + "manifest.json"
    if overwrite:
        store.put_bytes(man_key, body, content_type="application/json")
    else:
        head = store.head(man_key)
        if head is None:
            store.put_bytes(man_key, body, content_type="application/json", if_none_match="*")
        else:
            remote = store.get_bytes(man_key)
            if sha256_bytes(remote) != sha256_bytes(body):
                raise ConflictError(f"audio manifest already exists and differs for chapter {chapter}")
    written = store.get_bytes(man_key)
    if sha256_bytes(written) != sha256_bytes(body):
        raise ConflictError(f"audio manifest verification failed for chapter {chapter}")
    for name, meta in plan["files"].items():
        remote = store.get_bytes(dest_prefix + name)
        if len(remote) != meta["bytes"] or sha256_bytes(remote) != meta["sha256"]:
            raise ConflictError(f"final audio verification failed for {name}")
    return "published"


def recover_if_elected(store, dest_prefix: str) -> bool:
    """Finish a crashed publish from the elected attempt. Returns True when no new render is required."""
    man_key = dest_prefix + "manifest.json"
    if store.head(man_key) is not None:
        return True
    plan_key = dest_prefix + "publish.json"
    if store.head(plan_key) is None:
        return False
    plan = json.loads(store.get_bytes(plan_key).decode())
    attempt = plan["attempt_prefix"]
    for name, meta in plan["files"].items():
        try:
            body = store.get_bytes(attempt + name)
        except Exception as exc:
            raise ConflictError(f"elected audio attempt is incomplete at {name}; not rendering a second version") from exc
        if len(body) != meta["bytes"] or sha256_bytes(body) != meta["sha256"]:
            raise ConflictError(f"elected audio attempt hash mismatch at {name}")
        final_key = dest_prefix + name
        head = store.head(final_key)
        if head is None:
            store.put_bytes(final_key, body, content_type="audio/ogg", if_none_match="*")
        elif head.size != len(body):
            raise ConflictError(f"partial audio at {final_key} does not match the election")
    manifest_body = store.get_bytes(attempt + "manifest.json")
    if "manifest_sha256" in plan and sha256_bytes(manifest_body) != plan["manifest_sha256"]:
        raise ConflictError("elected manifest hash mismatch")
    if store.head(man_key) is None:
        store.put_bytes(man_key, manifest_body, content_type="application/json", if_none_match="*")
    return True
