"""In-memory R2 tests for leases, completion, registry, and device policy."""

from __future__ import annotations

import json
import threading
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.gpu_tts.student_tts.distributed.audio_publish import publish_chapter_audio
from scripts.gpu_tts.student_tts.distributed.audio_stage import chapters_to_render
from scripts.gpu_tts.student_tts.distributed.batching import call_reducing, resolve_batch
from scripts.gpu_tts.student_tts.distributed.chapters import pending_chapters
from scripts.gpu_tts.student_tts.distributed.cli import build_parser, main, parse_args
from scripts.gpu_tts.student_tts.distributed.deviceutil import select_device
from scripts.gpu_tts.student_tts.distributed.exitcodes import EXIT_SETUP
from scripts.gpu_tts.student_tts.distributed.hashing import HashMismatch, commit_download, sha256_bytes, sha256_file
from scripts.gpu_tts.student_tts.distributed.layout import (
    LEASE_TTL_S,
    SOURCE_SCHEMA,
    TAGGER_MODEL,
    audio_prefix,
    chapter_text_key,
    lease_key,
    source_manifest_key,
)
from scripts.gpu_tts.student_tts.distributed.leases import Heartbeat, claim_chapter
from scripts.gpu_tts.student_tts.distributed.orchestrate import execute
from scripts.gpu_tts.student_tts.distributed.publish import publish_novel
from scripts.gpu_tts.student_tts.distributed.redact import clear, install, redact
from scripts.gpu_tts.student_tts.distributed.registry import RegistryError, empty_index, load_index, remedy, resolve_novel, slugify, upsert_novel
from scripts.gpu_tts.student_tts.distributed.store import ConflictError, FakeStore
from scripts.gpu_tts.student_tts.distributed.style_plan import pick_style, plan_paragraph, silence_seconds
from scripts.gpu_tts.student_tts.distributed.tag_stage import chapters_to_tag, publish_tag, tag_manifest_body
from scripts.gpu_tts.student_tts.distributed.tagger_logic import HEADS, labels_from_sums
from scripts.gpu_tts.student_tts.distributed.textutil import raw_paragraphs
from scripts.gpu_tts.student_tts.distributed.validate import classify_audio, classify_tag


SLUG = "gap-book"


def _claim(store, owner, *, now, run_id="run", worker_index=0, stage="tag", chapter=19):
    return claim_chapter(
        store,
        slug=SLUG,
        stage=stage,
        chapter=chapter,
        owner=owner,
        run_id=run_id,
        worker_index=worker_index,
        runtime_version="2026-10-20.1",
        now=now,
        host="test-host",
        pid=1,
    )


def _source(store, texts: dict[int, str]) -> dict:
    rows = []
    for chapter, text in texts.items():
        body = text.encode()
        key = chapter_text_key(SLUG, chapter)
        store.put_bytes(key, body, content_type="text/plain")
        rows.append({
            "chapter": chapter,
            "key": key,
            "sha256": sha256_bytes(body),
            "bytes": len(body),
            "paragraphs": len(raw_paragraphs(text)),
            "title": f"Chapter {chapter}",
        })
    source = {"schema": SOURCE_SCHEMA, "name": "Gap Book", "slug": SLUG, "source_version": "v1", "chapters": rows}
    store.put_bytes(
        source_manifest_key(SLUG),
        (json.dumps(source, sort_keys=True) + "\n").encode(),
        content_type="application/json",
    )
    return source


def _tag_payload() -> bytes:
    row = {
        "paragraph": 0,
        "emotion": "awe",
        "style": "none",
        "sfx": "none",
        "confidence": 0.847,
        "style_conf": 0.965,
        "sfx_conf": 0.916,
    }
    return (json.dumps(row) + "\n").encode()


def _publish_one_tag(store, source, chapter: int) -> dict:
    spec = next(row for row in source["chapters"] if row["chapter"] == chapter)
    payload = _tag_payload()
    manifest = tag_manifest_body(
        source_chapter={"slug": SLUG, "chapter": chapter, "sha256": spec["sha256"], "bytes": spec["bytes"]},
        paragraph_count=spec["paragraphs"],
        digest="",
        size=0,
        blob_key="",
        batch_requested=4,
        batch_effective=4,
        worker_id="test",
    )
    assert publish_tag(store, SLUG, chapter, payload, manifest) == "published"
    return classify_tag(store, source, chapter, now=0)


def _audio_files() -> dict[str, bytes]:
    return {"title.opus": b"title-bytes", "0.opus": b"paragraph-bytes", "chapter.opus": b"chapter-bytes"}


def _audio_manifest(spec: dict, tag_row: dict, files: dict[str, bytes]) -> dict:
    def entry(name: str) -> dict:
        body = files[name]
        return {"file": name, "bytes": len(body), "sha256": sha256_bytes(body)}

    return {
        "source_sha256": spec["sha256"],
        "tag_manifest_sha256": tag_row["manifest_sha256"],
        "tagger_version": TAGGER_MODEL,
        "title": entry("title.opus"),
        "paragraphs": [entry("0.opus")],
        "chapter_file": entry("chapter.opus"),
    }


def test_two_workers_one_lease_wins():
    store = FakeStore()
    barrier = threading.Barrier(2)
    found = []

    def race(owner: str) -> None:
        barrier.wait()
        found.append(_claim(store, owner, now=1.0, run_id=owner))

    threads = [threading.Thread(target=race, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [item for item in found if item is not None]
    assert len(winners) == 1
    assert store.delete_calls == []


def test_heartbeat_and_stale_takeover_uses_etag():
    store = FakeStore()
    first = _claim(store, "owner-a", now=0.0, run_id="a")
    assert first is not None
    assert first.heartbeat(30.0)
    assert first.lease["updated_unix"] == 30.0
    assert _claim(store, "owner-b", now=30.0 + LEASE_TTL_S - 1, run_id="b") is None
    second = _claim(store, "owner-b", now=30.0 + LEASE_TTL_S, run_id="b")
    assert second is not None
    assert second.lease["owner"] == "owner-b"
    assert first.heartbeat(30.0 + LEASE_TTL_S + 1) is False
    assert store.delete_calls == []


def test_heartbeat_thread_refreshes_without_delete():
    store = FakeStore()
    claim = _claim(store, "owner-a", now=1.0)
    clock = {"now": 1.0}

    def now() -> float:
        clock["now"] += 1.0
        return clock["now"]

    with Heartbeat(claim, now, interval=0.01):
        threading.Event().wait(0.08)
    assert claim.lease["updated_unix"] > 1.0
    assert store.delete_calls == []


def test_timeout_before_write_does_not_claim_and_after_write_reconciles():
    store = FakeStore()
    store.timeout_mode = "before"
    assert _claim(store, "owner-a", now=1.0) is None
    assert store.head(lease_key(SLUG, "tag", 19)) is None

    store.timeout_mode = "after"
    claim = _claim(store, "owner-a", now=2.0)
    assert claim is not None
    assert json.loads(store.get_bytes(lease_key(SLUG, "tag", 19)).decode())["owner"] == "owner-a"

    store.timeout_mode = "after"
    assert claim.heartbeat(3.0)
    assert claim.lease["updated_unix"] == 3.0


def test_tag_manifest_is_last_and_gaps_are_scanned():
    store = FakeStore()
    source = _source(store, {18: "Eighteen.", 19: "Nineteen.", 20: "Twenty."})
    before = len(store.puts)
    row18 = _publish_one_tag(store, source, 18)
    assert store.puts[before].endswith(".jsonl") or "blobs/" in store.puts[before]
    assert store.puts[-1].endswith("chapter_00018.manifest.json")
    assert row18["status"] == "valid"
    _publish_one_tag(store, source, 20)
    work, rows = chapters_to_tag(store, source, [18, 19, 20], now=0)
    assert work == [19]
    assert pending_chapters([18, 19, 20], {18, 20}) == [19]
    by_status = {row["chapter"]: row["status"] for row in rows}
    assert by_status == {18: "valid", 19: "missing", 20: "valid"}
    spec = next(item for item in source["chapters"] if item["chapter"] == 18)
    other = _tag_payload().replace(b"awe", b"fear")
    manifest = tag_manifest_body(
        source_chapter={"slug": SLUG, "chapter": 18, "sha256": spec["sha256"], "bytes": spec["bytes"]},
        paragraph_count=1, digest="", size=0, blob_key="", batch_requested=1, batch_effective=1, worker_id="x",
    )
    with pytest.raises(ConflictError):
        publish_tag(store, SLUG, 18, other, manifest)


def test_audio_completion_skips_present_and_refuses_partial_mismatch():
    store = FakeStore()
    source = _source(store, {7: "Seven."})
    tag_row = _publish_one_tag(store, source, 7)
    spec = source["chapters"][0]
    files = _audio_files()
    prefix = audio_prefix(SLUG, 7)
    assert publish_chapter_audio(
        store, slug=SLUG, chapter=7, dest_prefix=prefix, files=files,
        manifest=_audio_manifest(spec, tag_row, files), attempt_id="attempt-1",
    ) == "published"
    assert store.puts[-1] == prefix + "manifest.json"
    audio = classify_audio(store, source, 7, now=0, tag_row=tag_row)
    assert audio["status"] == "valid"
    work, _rows = chapters_to_render(store, source, [7], [tag_row], now=0, allow_partial=False)
    assert work == []

    clash = FakeStore()
    source = _source(clash, {7: "Seven."})
    tag_row = _publish_one_tag(clash, source, 7)
    prefix = audio_prefix(SLUG, 7)
    clash.put_bytes(prefix + "title.opus", b"not-the-title", content_type="audio/ogg", if_none_match="*")
    assert classify_audio(clash, source, 7, now=0, tag_row=tag_row)["status"] == "conflict"
    with pytest.raises(ConflictError):
        publish_chapter_audio(
            clash, slug=SLUG, chapter=7, dest_prefix=prefix, files=files,
            manifest=_audio_manifest(spec, tag_row, files), attempt_id="attempt-2",
        )


def test_identical_partial_audio_can_be_finished():
    store = FakeStore()
    source = _source(store, {7: "Seven."})
    tag_row = _publish_one_tag(store, source, 7)
    spec = source["chapters"][0]
    files = _audio_files()
    prefix = audio_prefix(SLUG, 7)
    for name, body in files.items():
        store.put_bytes(prefix + name, body, content_type="audio/ogg", if_none_match="*")
    assert publish_chapter_audio(
        store, slug=SLUG, chapter=7, dest_prefix=prefix, files=files,
        manifest=_audio_manifest(spec, tag_row, files), attempt_id="attempt-same",
    ) == "published"
    assert classify_audio(store, source, 7, now=0, tag_row=tag_row)["status"] == "valid"


def test_registry_remedy_and_slug_ambiguity():
    assert slugify("The Eye of the World") == "the-eye-of-the-world"
    with pytest.raises(RegistryError) as caught:
        resolve_novel(empty_index(), "The Eye of the World")
    assert str(caught.value) == remedy("The Eye of the World")
    assert 'publish-novel --name "The Eye of the World" --source-dir <directory>' in str(caught.value)
    index = upsert_novel(empty_index(), {"name": "One", "slug": "one", "aliases": ["shared"]})
    index = upsert_novel(index, {"name": "Two", "slug": "two", "aliases": ["shared"]})
    with pytest.raises(RegistryError, match="multiple registry slugs"):
        resolve_novel(index, "shared")


def test_unregistered_run_exits_with_remedy(capsys):
    args = parse_args(["run", "--novel", "The Eye of the World", "--stage", "tag"])
    code = execute(args, FakeStore(), inline=True)
    assert code == EXIT_SETUP
    assert remedy("The Eye of the World") in capsys.readouterr().out


def test_credentials_are_redacted_and_not_cli_flags():
    install(["supersecretvalue"])
    try:
        assert redact("endpoint supersecretvalue tail") == "endpoint *** tail"
        help_text = build_parser().format_help()
        assert "R2_SECRET" not in help_text
        assert "access_key" not in help_text
        rendered = parse_args(["--novel", "Book", "--stage", "render"])
        assert rendered.stage == "audio"
        assert rendered.command == "run"
        assert main(["run", "--novel", "Book", "--workers", "0"], store=FakeStore()) == EXIT_SETUP
    finally:
        clear()


def test_hash_verification_discards_partial(tmp_path: Path):
    body = b"artifact-bytes"
    good = tmp_path / "good.bin"
    partial = good.with_suffix(good.suffix + ".partial")
    partial.write_bytes(body)
    commit_download(partial, good, sha256_bytes(body), len(body))
    digest, size = sha256_file(good)
    assert digest == sha256_bytes(body) and size == len(body)
    bad = tmp_path / "bad.bin.partial"
    bad.write_bytes(b"wrong")
    with pytest.raises(HashMismatch):
        commit_download(bad, tmp_path / "bad.bin", sha256_bytes(body), len(body))
    assert not bad.exists()


def test_oom_fallback_never_increases_batch_and_device_order():
    def explode(batch: int):
        if batch > 1:
            raise RuntimeError("CUDA out of memory")
        return "ok"

    value, batch, tried = call_reducing(4, explode)
    assert value == "ok" and batch == 1 and tried == [4, 2, 1]
    with pytest.raises(RuntimeError):
        call_reducing(1, lambda _batch: (_ for _ in ()).throw(RuntimeError("out of memory")))

    audio, reason = resolve_batch("audio", "cpu", batch_size=None, tag_batch_size=None, audio_batch_size=None, profile=None, model_key="m")
    assert (audio, reason) == (1, "default")
    lowered, reason = resolve_batch(
        "audio", "cuda", batch_size=None, tag_batch_size=None, audio_batch_size=None,
        profile={"audio": {"cuda": {"m": 2}}}, model_key="m",
    )
    assert (lowered, reason) == (2, "machine-profile")
    explicit, reason = resolve_batch(
        "audio", "cuda", batch_size=4, tag_batch_size=None, audio_batch_size=None,
        profile={"audio": {"cuda": {"m": 1}}}, model_key="m",
    )
    assert (explicit, reason) == (4, "global-flag")

    class Cuda:
        def is_available(self):
            return True

    class TorchCuda:
        cuda = Cuda()

    class TorchMps:
        class cuda:
            @staticmethod
            def is_available():
                return False

        class backends:
            class mps:
                @staticmethod
                def is_available():
                    return True

    class TorchCpu:
        class cuda:
            @staticmethod
            def is_available():
                return False

        class backends:
            class mps:
                @staticmethod
                def is_available():
                    return False

    assert select_device("auto", TorchCuda()) == "cuda"
    assert select_device("auto", TorchMps()) == "mps"
    assert select_device("auto", TorchCpu()) == "cpu"
    assert select_device("cpu", TorchCuda()) == "cpu"


def test_ensemble_average_and_neutral_style_fallback():
    emotion = [0.0] * len(HEADS["emotion"])
    emotion[0] = 1.4
    emotion[4] = 0.6
    style = [1.6, 0.2, 0.2]
    sfx = [1.9] + [0.0] * (len(HEADS["sfx"]) - 1)
    sfx[1] = 0.1
    rows = labels_from_sums({"emotion": [emotion], "style": [style], "sfx": [sfx]}, 2)
    assert rows[0]["emotion"] == "neutral"
    assert rows[0]["confidence"] == 0.7
    assert rows[0]["style"] == "none"
    picked = pick_style({"narrator/neutral": [0]}, [{"clip": "n"}], "narrator", "awe", (1, 0))
    assert picked["fallback"] is True
    assert picked["requested"] == "narrator/awe"
    assert silence_seconds("ReadNovelFull.me") == 0.3
    assert plan_paragraph("ReadNovelFull.me", {"emotion": "neutral", "style": "none", "sfx": "none"}) == []


def test_namespace_prefixes_keys_and_publish_refuses_it(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AUDIOBOOK_WORKER_NAMESPACE", "scratch-smoke")
    assert lease_key(SLUG, "audio", 3).startswith("scratch-smoke/student_tts/coord/")
    assert audio_prefix(SLUG, 3).startswith("scratch-smoke/audio-student/")
    monkeypatch.setenv("AUDIOBOOK_WORKER_NAMESPACE", "scratch-smoke")
    code = publish_novel(Namespace(name="Example", source_dir=tmp_path, tags_dir=None, chapters="1", slug="example"), FakeStore())
    assert code == EXIT_SETUP


def test_eye_preview_chapter_is_excluded(tmp_path: Path):
    book = tmp_path / "book"
    tags = book / "tags"
    (book / "chapters").mkdir(parents=True)
    tags.mkdir()
    (book / "titles.json").write_text(json.dumps({"54": "Title 54", "55": "Preview"}))
    for chapter in (54, 55):
        (book / "chapters" / f"{chapter:05d}.txt").write_text(f"Chapter {chapter} text.")
        (tags / f"{chapter:05d}.jsonl").write_text(
            json.dumps({
                "paragraph": 0, "emotion": "neutral", "style": "none", "sfx": "none",
                "confidence": 1, "style_conf": 1, "sfx_conf": 1,
            }) + "\n"
        )
    store = FakeStore()
    code = publish_novel(
        Namespace(name="The Eye of the World", source_dir=book, tags_dir=tags, chapters="54-55", slug=None),
        store,
    )
    assert code == 0
    novel = load_index(store)["novels"][0]
    assert novel["chapters"] == [54]
    assert store.head(chapter_text_key("the-eye-of-the-world", 55)) is None
    assert store.head(chapter_text_key("the-eye-of-the-world", 54)) is not None
