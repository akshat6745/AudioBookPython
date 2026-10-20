"""Publish private runtime artifacts and novel source. Never writes listener audio keys."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .exitcodes import EXIT_INVALID, EXIT_OK, EXIT_SETUP
from .hashing import sha256_bytes, sha256_file, sha256_iter
from .layout import (
    KNOWN_BYTES,
    KNOWN_SHA256,
    SOURCE_SCHEMA,
    SOURCE_VERSION,
    STYLETTS2_COMMIT,
    STYLETTS_RUNTIME,
    STYLETTS_VERSION,
    TAGGER_MODEL,
    TAGGER_RUNTIME,
    TAGGER_VERSION,
    VERIFIED_CHECKPOINT,
    assert_publish_key_allowed,
    source_manifest_key,
    styletts_artifact_key,
    styletts_manifest_key,
    tagger_artifact_key,
    tagger_manifest_key,
    titles_key,
)
from .registry import load_index, save_index, slugify, upsert_novel
from .store import ConflictError, PreconditionFailed
from .tag_stage import publish_tag, tag_manifest_body
from .textutil import raw_paragraphs, sha256_text


def _refuse_namespaced_publish() -> None:
    if os.environ.get("AUDIOBOOK_WORKER_NAMESPACE"):
        raise SystemExit("refusing to publish while AUDIOBOOK_WORKER_NAMESPACE is set")


def _connect(store):
    if store is not None:
        return store
    from .credentials import load_credentials
    from .store import BotoStore

    return BotoStore.connect(load_credentials(prompt=False))


def _put_new(store, key: str, body: bytes, content_type: str) -> str:
    assert_publish_key_allowed(key)
    head = store.head(key)
    if head is None:
        try:
            return store.put_bytes(key, body, content_type=content_type, if_none_match="*")
        except PreconditionFailed:
            head = store.head(key)
    if head is None or head.size != len(body):
        raise ConflictError(f"refusing to overwrite {key}")
    return head.etag


def _verify(store, key: str, digest: str, size: int) -> None:
    head = store.head(key)
    if head is None or head.size != size:
        raise ConflictError(f"verification failed for {key}")
    if hasattr(store, "iter_body") and size >= 64 * 1024 * 1024:
        actual, got = sha256_iter(store.iter_body(key))
    else:
        body = store.get_bytes(key)
        actual, got = sha256_bytes(body), len(body)
    if actual != digest or got != size:
        raise ConflictError(f"hash verification failed for {key}")


def _local_ok(path: Path, name: str) -> tuple[str, int]:
    digest, size = sha256_file(path)
    if name in KNOWN_SHA256 and (digest != KNOWN_SHA256[name] or size != KNOWN_BYTES[name]):
        raise SystemExit(f"{name} does not match the audited production hash")
    return digest, size


def _etag_matches(head_etag: str, expected: str) -> bool:
    return head_etag.replace('"', "") == expected.replace('"', "")


def publish_runtime(args, store=None) -> int:
    try:
        _refuse_namespaced_publish()
        store = _connect(store)
    except SystemExit as exc:
        print(exc)
        return EXIT_SETUP
    files = []
    try:
        print("verifying production checkpoint by size and streamed sha256", flush=True)
        checkpoint = _publish_checkpoint(store, args.checkpoint)
        files.append(checkpoint)
        mapping = {
            "checkpoint/config_ft.yml": args.config,
            "asr/epoch_00080.pth": args.asr,
            "asr/config.yml": args.asr_config,
            "jdc/bst.t7": args.jdc,
            "plbert/step_1000000.t7": args.plbert,
            "plbert/config.yml": args.plbert_config,
            "style_bank/bank.npz": args.bank_npz,
            "style_bank/bank.json": args.bank_json,
        }
        for name, path in mapping.items():
            print(f"publishing {name}", flush=True)
            files.append(_publish_file(store, styletts_artifact_key(name), path, name))
        _write_manifest(store, styletts_manifest_key(), {
            "schema": "runtime-manifest/v1",
            "runtime": STYLETTS_RUNTIME,
            "version": STYLETTS_VERSION,
            "styletts2_commit": STYLETTS2_COMMIT,
            "files": files,
        })
        tagger_files = []
        for name, path in (("deberta3large_teacher2", args.deberta_large), ("deberta3_base_slot", args.deberta_base)):
            logical = "deberta3large_teacher2" if "large" in name else "deberta3_teacher2"
            print(f"publishing tagger {logical}", flush=True)
            tagger_files.extend(_publish_tagger_dir(store, logical, path))
        _write_manifest(store, tagger_manifest_key(), {
            "schema": "runtime-manifest/v1",
            "runtime": TAGGER_RUNTIME,
            "version": TAGGER_VERSION,
            "models": TAGGER_MODEL,
            "files": tagger_files,
        })
    except (ConflictError, SystemExit, OSError) as exc:
        from .redact import redact

        print(redact(f"publish-runtime failed: {exc}"))
        return EXIT_INVALID
    except Exception as exc:
        from .redact import redact

        print(redact(f"publish-runtime failed: {type(exc).__name__}: {exc}"))
        return EXIT_INVALID
    total = sum(item["bytes"] for item in files)
    print(f"styletts runtime files {len(files)} bytes {total}; tagger files {len(tagger_files)}")
    return EXIT_OK


def _publish_checkpoint(store, path: Path) -> dict:
    digest, size = _local_ok(path, "checkpoint/epoch_2nd_00005.pth")
    key = VERIFIED_CHECKPOINT["key"]
    head = store.head(key)
    if head is None:
        raise ConflictError("production checkpoint is missing; refusing to upload a second copy blindly")
    if head.size != size:
        raise ConflictError("production checkpoint size differs; refusing to overwrite")
    # Multipart ETags are not content hashes. Stream the object and compare SHA256.
    multipart = "-" in head.etag.replace('"', "")
    if multipart or not _etag_matches(head.etag, VERIFIED_CHECKPOINT["etag"]):
        if hasattr(store, "iter_body"):
            actual, got = sha256_iter(store.iter_body(key))
        else:
            body = store.get_bytes(key)
            actual, got = sha256_bytes(body), len(body)
        if actual != digest or got != size:
            raise ConflictError("production checkpoint hash differs; refusing to overwrite")
    return {"name": "checkpoint/epoch_2nd_00005.pth", "key": key, "sha256": digest, "bytes": size, "referenced": True}


def _upload_path(store, key: str, path: Path, content_type: str = "application/octet-stream") -> None:
    size = path.stat().st_size
    head = store.head(key)
    if head is not None:
        if head.size != size:
            raise ConflictError(f"refusing to overwrite {key}")
        return
    assert_publish_key_allowed(key)
    if size >= 64 * 1024 * 1024 and hasattr(store, "put_file"):
        store.put_file(key, path, content_type=content_type, if_none_match="*")
        return
    store.put_bytes(key, path.read_bytes(), content_type=content_type, if_none_match="*")


def _publish_file(store, key: str, path: Path, logical: str) -> dict:
    digest, size = _local_ok(path, logical)
    _upload_path(store, key, path)
    _verify(store, key, digest, size)
    return {"name": logical, "key": key, "sha256": digest, "bytes": size}


def _publish_tagger_dir(store, name: str, path: Path) -> list[dict]:
    import tempfile

    config = json.loads((path / "config.json").read_text())
    uploaded = []
    config_key = tagger_artifact_key(f"{name}/student_config.json")
    config_bytes = (path / "config.json").read_bytes()
    _put_new(store, config_key, config_bytes, "application/json")
    digest, size = sha256_bytes(config_bytes), len(config_bytes)
    _verify(store, config_key, digest, size)
    uploaded.append({"name": f"{name}/student_config.json", "key": config_key, "sha256": digest, "bytes": size})
    weights = path / "model.safetensors"
    digest, size = sha256_file(weights)
    key = tagger_artifact_key(f"{name}/model.safetensors")
    _upload_path(store, key, weights)
    _verify(store, key, digest, size)
    uploaded.append({"name": f"{name}/model.safetensors", "key": key, "sha256": digest, "bytes": size})
    with tempfile.TemporaryDirectory(prefix="tagger-hf-") as temporary:
        hf = Path(temporary)
        _snapshot_tokenizer(config["base"], config.get("revision"), hf)
        for file_path in sorted(item for item in hf.rglob("*") if item.is_file()):
            relative = f"{name}/hf/{file_path.relative_to(hf).as_posix()}"
            digest, size = sha256_file(file_path)
            key = tagger_artifact_key(relative)
            _upload_path(store, key, file_path)
            _verify(store, key, digest, size)
            uploaded.append({"name": relative, "key": key, "sha256": digest, "bytes": size})
    return uploaded


def _snapshot_tokenizer(repo: str, revision: str | None, dest: Path) -> None:
    if (dest / "config.json").exists():
        return
    dest.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=repo,
        revision=revision,
        allow_patterns=["config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "spm.model", "added_tokens.json"],
        local_dir=str(dest),
    )


def _write_manifest(store, key: str, payload: dict) -> None:
    body = (json.dumps(payload, indent=1, sort_keys=True) + "\n").encode()
    head = store.head(key)
    if head is None:
        store.put_bytes(key, body, content_type="application/json", if_none_match="*")
        return
    existing = store.get_bytes(key)
    if sha256_bytes(existing) != sha256_bytes(body):
        raise ConflictError(f"runtime manifest {key} already differs; refusing to replace it")


def publish_novel(args, store=None) -> int:
    try:
        _refuse_namespaced_publish()
        store = _connect(store)
        from .chapters import parse_chapter_spec

        chapters = parse_chapter_spec(args.chapters)
        slug = args.slug or slugify(args.name)
        if slug == "the-eye-of-the-world":
            dropped = [chapter for chapter in chapters if chapter > 54]
            chapters = [chapter for chapter in chapters if chapter <= 54]
            if dropped:
                print(f"excluding preview chapters {dropped}; Eye source contract is chapters 1-54")
            if not chapters:
                raise SystemExit("no chapters left after excluding the Eye preview")
        summary = publish_novel_tree(store, name=args.name, slug=slug, source_dir=args.source_dir, chapters=chapters, tags_dir=args.tags_dir)
    except SystemExit as exc:
        print(exc)
        return EXIT_SETUP
    except (ConflictError, OSError, ValueError) as exc:
        print(f"publish-novel failed: {exc}")
        return EXIT_INVALID
    print(
        f"registered {summary['slug']} chapters {summary['chapters']} "
        f"source_bytes {summary['source_bytes']} tag_chapters {summary['tag_chapters']}"
    )
    return EXIT_OK


def publish_novel_tree(store, *, name: str, slug: str, source_dir: Path, chapters: list[int], tags_dir: Path | None = None) -> dict:
    source_dir = Path(source_dir)
    titles = json.loads((source_dir / "titles.json").read_text())
    rows = []
    source_bytes = 0
    for chapter in chapters:
        path = source_dir / "chapters" / f"{chapter:05d}.txt"
        text = path.read_text()
        body = path.read_bytes()
        paragraphs = raw_paragraphs(text)
        key = __import__("scripts.gpu_tts.student_tts.distributed.layout", fromlist=["chapter_text_key"]).chapter_text_key(slug, chapter)
        assert_publish_key_allowed(key)
        _put_new(store, key, body, "text/plain")
        _verify(store, key, sha256_text(text) if body == text.encode() else sha256_bytes(body), len(body))
        rows.append({
            "chapter": chapter,
            "key": key,
            "sha256": sha256_bytes(body),
            "bytes": len(body),
            "paragraphs": len(paragraphs),
            "title": titles.get(str(chapter), titles.get(chapter, f"Chapter {chapter}")),
        })
        source_bytes += len(body)
        if tags_dir is not None:
            tag_path = Path(tags_dir) / f"{chapter:05d}.jsonl"
            payload = tag_path.read_bytes()
            lines = [line for line in payload.decode().splitlines() if line.strip()]
            if len(lines) != len(paragraphs):
                raise ValueError(f"chapter {chapter} tag lines {len(lines)} != paragraphs {len(paragraphs)}")
            manifest = tag_manifest_body(
                source_chapter={"slug": slug, "chapter": chapter, "sha256": sha256_bytes(body), "bytes": len(body)},
                paragraph_count=len(lines), digest="", size=0, blob_key="",
                batch_requested=0, batch_effective=0, worker_id="publish-novel",
            )
            publish_tag(store, slug, chapter, payload, manifest)
    manifest = {
        "schema": SOURCE_SCHEMA,
        "name": name,
        "slug": slug,
        "source_version": SOURCE_VERSION,
        "chapters": rows,
    }
    _put_new(store, source_manifest_key(slug), (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode(), "application/json")
    _put_new(store, titles_key(slug), (json.dumps(titles, indent=1, sort_keys=True) + "\n").encode(), "application/json")
    index = upsert_novel(load_index(store), {
        "name": name,
        "slug": slug,
        "aliases": [],
        "source_version": SOURCE_VERSION,
        "chapters": chapters,
    })
    save_index(store, index)
    return {"slug": slug, "chapters": len(chapters), "source_bytes": source_bytes, "tag_chapters": 0 if tags_dir is None else len(chapters)}
