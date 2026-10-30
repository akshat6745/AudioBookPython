"""Behavior tests for the R2 worker. They do not load StyleTTS or DeBERTa weights."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.gpu_tts.student_tts.distributed import layout
from scripts.gpu_tts.student_tts.distributed.audio_publish import publish_chapter_audio
from scripts.gpu_tts.student_tts.distributed.batching import call_reducing, group_batches, resolve_batch
from scripts.gpu_tts.student_tts.distributed.chapters import pending_chapters, select_chapters
from scripts.gpu_tts.student_tts.distributed.cli import build_parser, main, parse_args
from scripts.gpu_tts.student_tts.distributed.credentials import Credentials, CredentialsError, load_credentials
from scripts.gpu_tts.student_tts.distributed.deviceutil import select_device
from scripts.gpu_tts.student_tts.distributed.exitcodes import EXIT_INCOMPLETE, EXIT_INVALID, EXIT_OK, EXIT_SETUP
from scripts.gpu_tts.student_tts.distributed.hashing import HashMismatch, commit_download, sha256_bytes
from scripts.gpu_tts.student_tts.distributed.leases import LEASE_TTL_S, claim_chapter, is_stale
from scripts.gpu_tts.student_tts.distributed.orchestrate import execute
from scripts.gpu_tts.student_tts.distributed.publish import publish_novel_tree
from scripts.gpu_tts.student_tts.distributed.redact import clear, install, redact
from scripts.gpu_tts.student_tts.distributed.registry import remedy
from scripts.gpu_tts.student_tts.distributed.report import write_report
from scripts.gpu_tts.student_tts.distributed.store import ConflictError, FakeStore
from scripts.gpu_tts.student_tts.distributed.style_plan import pick_style
from scripts.gpu_tts.student_tts.distributed.validate import classify_audio, load_source

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "scripts" / "gpu_tts" / "student_tts" / "distributed"


def label(index: int) -> dict:
    return {
        "paragraph": index, "emotion": "neutral", "style": "none", "sfx": "none",
        "confidence": 0.91, "style_conf": 0.95, "sfx_conf": 0.97,
    }


class FakeTagger:
    def __init__(self, oom: bool = False):
        self.batches: list[int] = []
        self.oom = oom
        self._lock = threading.Lock()

    def tag_chapter(self, rows, batch_size, device):
        with self._lock:
            self.batches.append(batch_size)
        if self.oom and batch_size > 1:
            raise RuntimeError("CUDA out of memory")
        return [{key: value for key, value in label(row["paragraph"]).items() if key != "paragraph"} for row in rows]


class Recorder:
    def __init__(self):
        self.calls = 0

    def render_and_publish(self, store, source, chapter, *args, **kwargs):
        self.calls += 1
        prefix = layout.audio_prefix(source["slug"], chapter)
        blob = b"OggS-fake"
        digest = sha256_bytes(blob)
        tag_man_sha = sha256_bytes(store.get_bytes(layout.tag_manifest_key(source["slug"], chapter)))
        spec = next(item for item in source["chapters"] if int(item["chapter"]) == int(chapter))
        publish_chapter_audio(
            store,
            slug=source["slug"],
            chapter=chapter,
            dest_prefix=prefix,
            files={"title.opus": blob, "0.opus": blob, "1.opus": blob, "chapter.opus": blob},
            manifest={
                "novel": source["slug"],
                "chapter": chapter,
                "source_sha256": spec["sha256"],
                "tag_manifest_sha256": tag_man_sha,
                "title": {"file": "title.opus", "bytes": len(blob), "sha256": digest},
                "paragraphs": [
                    {"file": "0.opus", "bytes": len(blob), "sha256": digest},
                    {"file": "1.opus", "bytes": len(blob), "sha256": digest},
                ],
                "chapter_file": {"file": "chapter.opus", "bytes": len(blob), "sha256": digest},
            },
            attempt_id="rec-1",
            overwrite=bool(kwargs.get("replace_stale")),
        )


def args(tmp: Path, **kwargs):
    base = dict(
        command="run", novel="The Eye of the World", stage="tag", workers=1, chapters=None,
        report_json=None, device="cpu", batch_size=None, tag_batch_size=None, audio_batch_size=None,
        validate=False, allow_partial_tags=False, reset_batch_profile=False, status_only=False, state=str(tmp),
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def write_book(tmp: Path, chapters: dict[int, str]) -> Path:
    root = tmp / "book"
    (root / "chapters").mkdir(parents=True)
    titles = {str(chapter): f"Title {chapter}" for chapter in chapters}
    (root / "titles.json").write_text(json.dumps(titles))
    for chapter, text in chapters.items():
        (root / "chapters" / f"{chapter:05d}.txt").write_text(text)
    return root


def test_cli_examples_and_secret_flags():
    tag = parse_args(["run", "--novel", "The Eye of the World", "--stage", "tag", "--workers", "4", "--batch-size", "32", "--validate"])
    assert (tag.stage, tag.workers, tag.batch_size, tag.validate) == ("tag", 4, 32, True)
    audio = parse_args(["run", "--novel", "The Eye of the World", "--stage", "audio", "--workers", "2", "--batch-size", "4", "--validate"])
    assert (audio.stage, audio.workers, audio.batch_size) == ("audio", 2, 4)
    alias = parse_args(["run", "--novel", "The Eye of the World", "--stage", "render"])
    assert alias.stage == "audio"
    assert parse_args(["validate", "--novel", "The Eye of the World", "--stage", "tag"]).command == "validate"
    assert parse_args(["--novel", "The Eye of the World"]).command == "run"
    flags = {flag for action in build_parser()._actions for flag in action.option_strings}
    for child in build_parser()._subparsers._group_actions:
        for sub in child.choices.values():
            flags.update(flag for action in sub._actions for flag in action.option_strings)
    assert "--secret" not in flags and "--access-key-id" not in flags and "--api-token" not in flags


def test_stage_modules_do_not_import_models():
    for relative in ("tag_stage.py", "audio_stage.py", "validate.py", "cli.py", "orchestrate.py"):
        tree = ast.parse((PKG / relative).read_text())
        modules = [node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)]
        assert not any("tts_engine" in module or "tagger_model" in module or "audio_render" in module for module in modules)


def test_audio_preflight_does_not_import_tts(tmp_path):
    script = r'''
import sys
from pathlib import Path
from scripts.gpu_tts.student_tts.distributed.orchestrate import execute
from scripts.gpu_tts.student_tts.distributed.publish import publish_novel_tree
from scripts.gpu_tts.student_tts.distributed.store import FakeStore
store = FakeStore()
root = Path(sys.argv[1]) / "book"
(root / "chapters").mkdir(parents=True)
(root / "titles.json").write_text('{"1": "Title"}')
(root / "chapters" / "00001.txt").write_text("Hello there.\n\nSecond paragraph.")
publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=root, chapters=[1])
from types import SimpleNamespace
ns = SimpleNamespace(command="run", novel="The Eye of the World", stage="audio", workers=1, chapters=None,
    report_json=None, device="cpu", batch_size=None, tag_batch_size=None, audio_batch_size=None,
    validate=False, allow_partial_tags=False, reset_batch_profile=False, status_only=False, state=sys.argv[1])
code = execute(ns, store, inline=True)
assert code == 2, code
banned = ("tts_engine", "tagger_model", "audio_render")
assert not any(any(part in name for part in banned) for name in sys.modules)
print("ok")
'''
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=ROOT, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_credentials_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.gpu_tts.student_tts.distributed.credentials.Path.home", lambda: tmp_path / "home")
    with pytest.raises(CredentialsError) as raised:
        load_credentials(environ={}, cwd=tmp_path, prompt=False)
    assert "supersecretvalue" not in str(raised.value)
    assert "Cloudflare API token alone is not enough" in str(raised.value)
    install(["supersecretvalue"])
    path = tmp_path / "report.json"
    write_report(path, {"novel": "x", "detail": "leak supersecretvalue"})
    assert "supersecretvalue" not in path.read_text()
    assert redact("supersecretvalue") == "***"


HELP_ARGV = (
    ["--help"],
    ["-h"],
    ["run", "--help"],
    ["run", "-h"],
    ["validate", "--help"],
    ["status", "--help"],
    ["setup", "--help"],
    ["publish-runtime", "--help"],
    ["publish-novel", "--help"],
    ["--non-interactive", "--help"],
)

SECRET = "prompted-secret-value"


def _isolate_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("scripts.gpu_tts.student_tts.distributed.credentials.Path.home", lambda: tmp_path / "home")
    for key in ("R2_ENDPOINT_URL", "R2_BUCKET_NAME", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "AUDIOBOOK_R2_ENV"):
        monkeypatch.delenv(key, raising=False)


def test_help_exits_zero_without_credentials_or_r2(monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise AssertionError("help loaded credentials or opened R2")

    monkeypatch.setattr("scripts.gpu_tts.student_tts.distributed.credentials.load_credentials", boom)
    monkeypatch.setattr("scripts.gpu_tts.student_tts.distributed.store.BotoStore.connect", boom)
    for argv in HELP_ARGV:
        assert main(argv) == EXIT_OK, argv
        captured = capsys.readouterr()
        assert "usage:" in captured.out
        assert SECRET not in captured.out + captured.err
    top = main(["--help"])
    assert top == EXIT_OK
    top_out = capsys.readouterr().out
    assert "publish-novel" in top_out and "--non-interactive" in top_out
    assert main(["run", "--help"]) == EXIT_OK
    run_out = capsys.readouterr().out
    assert "--batch-size" in run_out and "--non-interactive" in run_out
    assert main(["run"]) == 2
    assert main([]) == EXIT_SETUP
    assert main(["run", "--novel", "Book", "--workers", "0"], store=FakeStore()) == EXIT_SETUP


def test_help_process_exit_codes():
    for argv in (["--help"], ["run", "--help"], ["validate", "--help"], ["status", "--help"], ["setup", "--help"], ["publish-runtime", "--help"], ["publish-novel", "--help"]):
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.gpu_tts.student_tts.distributed", *argv],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        assert proc.returncode == 0, (argv, proc.returncode, proc.stderr)
        assert "usage:" in proc.stdout
        assert SECRET not in proc.stdout + proc.stderr


def test_interactive_prompt_and_noninteractive_failure(tmp_path, monkeypatch, capsys):
    _isolate_credentials(monkeypatch, tmp_path)
    calls = []

    def fake_input(prompt=""):
        calls.append(("input", prompt))
        mapping = {
            "endpoint": "https://example.r2.cloudflarestorage.com",
            "bucket": "bucket-name",
            "Access Key ID": "AKIDEXAMPLE",
        }
        for needle, value in mapping.items():
            if needle in prompt:
                return value
        raise AssertionError(prompt)

    def fake_getpass(prompt=""):
        calls.append(("getpass", prompt))
        return SECRET

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("getpass.getpass", fake_getpass)
    try:
        creds = load_credentials(environ={}, cwd=tmp_path, prompt=None, tty=True)
        assert creds.endpoint_url == "https://example.r2.cloudflarestorage.com"
        assert creds.bucket == "bucket-name"
        assert creds.access_key_id == "AKIDEXAMPLE"
        assert creds.secret_access_key == SECRET
        assert repr(creds) == "Credentials(redacted)"
        assert [kind for kind, _prompt in calls] == ["input", "input", "input", "getpass"]
        assert all("API token" in prompt for _kind, prompt in calls if _kind == "getpass")
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err
        assert "not enough for S3" in captured.err
        report = tmp_path / "report.json"
        write_report(report, {"detail": f"leak {SECRET}", "novel": "x"})
        assert SECRET not in report.read_text()
        log_path = tmp_path / "worker.log"
        log_path.write_text(redact(f"worker saw {SECRET}\n"))
        assert SECRET not in log_path.read_text()
    finally:
        clear()

    calls.clear()
    filled = {
        "R2_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
        "R2_BUCKET_NAME": "bucket-name",
        "R2_ACCESS_KEY_ID": "AKIDEXAMPLE",
        "R2_SECRET_ACCESS_KEY": SECRET,
    }
    load_credentials(environ=filled, cwd=tmp_path, prompt=None, tty=True)
    assert calls == []
    partial = dict(filled)
    del partial["R2_SECRET_ACCESS_KEY"]
    try:
        creds = load_credentials(environ=partial, cwd=tmp_path, prompt=None, tty=True)
        assert creds.secret_access_key == SECRET
        assert [kind for kind, _prompt in calls] == ["getpass"]
    finally:
        clear()
    calls.clear()
    file_dir = tmp_path / "fromfile"
    file_dir.mkdir()
    (file_dir / ".env").write_text("\n".join(f'{key}="{value}"' for key, value in filled.items()) + "\n")
    load_credentials(environ={}, cwd=file_dir, prompt=None, tty=True)
    assert calls == []
    clear()

    with pytest.raises(CredentialsError) as raised:
        load_credentials(environ={}, cwd=tmp_path / "empty", prompt=None, tty=False)
    assert "R2_SECRET_ACCESS_KEY" in str(raised.value)
    assert SECRET not in str(raised.value)
    assert calls == []
    with pytest.raises(CredentialsError):
        load_credentials(environ={}, cwd=tmp_path / "empty", prompt=False, tty=True)
    assert calls == []

    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    captured_creds = {}

    def connect(creds):
        captured_creds["creds"] = creds
        return FakeStore()

    monkeypatch.setattr("scripts.gpu_tts.student_tts.distributed.store.BotoStore.connect", connect)
    code = main(["--non-interactive", "run", "--novel", "Book"])
    assert code == EXIT_SETUP
    assert "creds" not in captured_creds
    text = "".join(capsys.readouterr())
    assert "R2_ENDPOINT_URL" in text and SECRET not in text
    code = main(["run", "--novel", "Book"])
    assert captured_creds["creds"].secret_access_key == SECRET
    assert repr(captured_creds["creds"]) == "Credentials(redacted)"
    text = "".join(capsys.readouterr())
    assert SECRET not in text
    clear()


def test_prompted_credentials_stay_in_memory(tmp_path, monkeypatch):
    store = FakeStore()
    book = write_book(tmp_path, {1: "Hello there."})
    publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=book, chapters=[1])
    creds = Credentials("https://example.r2.cloudflarestorage.com", "bucket-name", "AKIDEXAMPLE", SECRET)
    captured = {}

    def fake_run(stage, count, payload):
        captured["payload"] = payload
        captured["stage"] = stage
        return 0

    monkeypatch.setattr("scripts.gpu_tts.student_tts.distributed.orchestrate.run_processes", fake_run)
    code = execute(args(tmp_path, stage="tag"), store, inline=False, credentials=creds)
    assert code == EXIT_OK
    assert captured["payload"]["credentials"] is creds
    visible = {key: value for key, value in captured["payload"].items() if key != "credentials"}
    assert SECRET not in repr(visible)


def test_registry_remedy_and_range(tmp_path):
    message = remedy("The Eye of the World")
    assert 'audiobook-worker publish-novel --name "The Eye of the World" --source-dir <directory>' in message
    store = FakeStore()
    assert execute(args(tmp_path), store, inline=True) == EXIT_SETUP
    book = write_book(tmp_path, {1: "Only one."})
    publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=book, chapters=[1])
    assert execute(args(tmp_path, chapters="1-2"), store, inline=True) == EXIT_SETUP
    assert select_chapters("18-20", [18, 19, 20]) == [18, 19, 20]
    assert pending_chapters([18, 19, 20], {18, 20}) == [19]


def test_auto_discovers_app_uploaded_novel_from_r2(tmp_path):
    import gzip

    store = FakeStore()
    gz_body = gzip.compress(b"First paragraph of Underlord.\n\nSecond paragraph.")
    store.put_bytes("novels/underlord/chapter_1.txt.gz", gz_body, content_type="application/gzip")
    tagger = FakeTagger()
    code = execute(args(tmp_path, novel="underlord", stage="tag", batch_size=16, validate=True), store, tagger=tagger, inline=True)
    assert code == EXIT_OK
    assert tagger.batches == [16]
    source = load_source(store, "underlord")
    assert len(source["chapters"]) == 1
    assert source["chapters"][0]["paragraphs"] == 2


def test_tag_gap_workers_batch_and_validate(tmp_path):
    store = FakeStore()
    texts = {18: "Eighteen.", 19: "Nineteen is missing.", 20: "Twenty."}
    book = write_book(tmp_path, texts)
    publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=book, chapters=[18, 19, 20])
    tagger = FakeTagger()
    code = execute(args(tmp_path, stage="tag", chapters="18,20", batch_size=32, workers=1), store, tagger=tagger, inline=True)
    assert code == EXIT_OK
    assert tagger.batches == [32, 32]
    tagger.batches.clear()
    code = execute(args(tmp_path, stage="tag", workers=2, batch_size=32, validate=True, report_json=tmp_path / "tags.json"), store, tagger=tagger, inline=True)
    assert code == EXIT_OK
    assert tagger.batches == [32]
    report = json.loads((tmp_path / "tags.json").read_text())
    assert report["counts"]["valid"] == 3
    assert report["exit_code"] == 0


def test_oom_halves_batch(tmp_path):
    store = FakeStore()
    book = write_book(tmp_path, {1: "Hello."})
    publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=book, chapters=[1])
    tagger = FakeTagger(oom=True)
    assert execute(args(tmp_path, batch_size=4), store, tagger=tagger, inline=True) == EXIT_OK
    assert tagger.batches == [4, 2, 1]


def test_two_workers_do_not_share_a_chapter(tmp_path):
    store = FakeStore()
    texts = {index: f"Chapter {index} text." for index in range(1, 5)}
    book = write_book(tmp_path, texts)
    publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=book, chapters=list(texts))
    tagger = FakeTagger()
    assert execute(args(tmp_path, workers=3, batch_size=8), store, tagger=tagger, inline=True) == EXIT_OK
    assert len(tagger.batches) == 4


def test_lease_race_heartbeat_takeover_and_timeout():
    store = FakeStore()
    winners = []
    barrier = threading.Barrier(2)

    def race(index):
        barrier.wait()
        claim = claim_chapter(
            store, slug="book", stage="tag", chapter=1, owner=f"owner-{index}", run_id="run",
            worker_index=index, runtime_version="v", now=0, host="host-a", pid=index,
        )
        winners.append(claim is not None)

    threads = [threading.Thread(target=race, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert winners.count(True) == 1
    body = json.loads(store.get_bytes(layout.lease_key("book", "tag", 1)).decode())
    claim = claim_chapter(
        store, slug="book", stage="tag", chapter=1, owner=body["owner"], run_id=body["run_id"],
        worker_index=body["worker_index"], runtime_version="v", now=10, host=body["host"], pid=1,
    )
    assert claim is not None
    assert claim.heartbeat(70)
    assert is_stale(claim.lease, 70 + LEASE_TTL_S)
    stolen = claim_chapter(
        store, slug="book", stage="tag", chapter=1, owner="other", run_id="run-2", worker_index=0,
        runtime_version="v", now=70 + LEASE_TTL_S, host="host-b", pid=2,
    )
    assert stolen is not None and stolen.lease["host"] == "host-b"
    assert claim.heartbeat(71) is False
    assert store.delete_calls == []
    store.timeout_mode = "after"
    timed = claim_chapter(
        store, slug="book", stage="audio", chapter=3, owner="timeout", run_id="run-t", worker_index=0,
        runtime_version="v", now=5, host="host-a", pid=9,
    )
    assert timed is not None and timed.lease["owner"] == "timeout"
    assert timed.release(6)
    assert json.loads(store.get_bytes(timed.key).decode())["status"] == "released"


def test_audio_refuses_partial_tags_and_skips_legacy(tmp_path):
    store = FakeStore()
    book = write_book(tmp_path, {1: "Hello.\n\nAgain."})
    publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=book, chapters=[1])
    recorder = Recorder()
    assert execute(args(tmp_path, stage="audio"), store, renderer=recorder, inline=True) == EXIT_INCOMPLETE
    assert recorder.calls == 0
    tagger = FakeTagger()
    assert execute(args(tmp_path, stage="tag", batch_size=32), store, tagger=tagger, inline=True) == EXIT_OK
    source = load_source(store, "the-eye-of-the-world")
    prefix = layout.audio_prefix("the-eye-of-the-world", 1)
    blob = b"OggS-fake"
    digest = sha256_bytes(blob)
    manifest = {
        "title": {"file": "title.opus", "bytes": len(blob), "sha256": digest},
        "paragraphs": [
            {"file": "0.opus", "bytes": len(blob), "sha256": digest},
            {"file": "1.opus", "bytes": len(blob), "sha256": digest},
        ],
        "chapter_file": {"file": "chapter.opus", "bytes": len(blob), "sha256": digest},
    }
    for name in ("title.opus", "0.opus", "1.opus", "chapter.opus"):
        store.put_bytes(prefix + name, blob, content_type="audio/ogg")
    store.put_bytes(prefix + "manifest.json", (json.dumps(manifest) + "\n").encode(), content_type="application/json")
    assert classify_audio(store, source, 1, now=0, tag_row={"status": "valid", "manifest_sha256": "x"})["status"] == "legacy_valid"
    report_path = tmp_path / "audio.json"
    assert execute(args(tmp_path, stage="audio", validate=True, report_json=report_path), store, renderer=recorder, inline=True) == EXIT_OK
    assert recorder.calls == 0
    assert json.loads(report_path.read_text())["counts"]["legacy_valid"] == 1
    assert execute(args(tmp_path, stage="audio", replace_stale=True), store, renderer=recorder, inline=True) == EXIT_OK
    assert recorder.calls == 1


def test_background_audio_publish_pipeline_overlaps_next_chapter(tmp_path):
    store = FakeStore()
    book = write_book(tmp_path, {1: "Chapter one.", 2: "Chapter two."})
    publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=book, chapters=[1, 2])
    tagger = FakeTagger()
    assert execute(args(tmp_path, stage="tag", batch_size=16), store, tagger=tagger, inline=True) == EXIT_OK

    events = []
    ch1_encode_gate = threading.Event()
    ch2_synth_started = threading.Event()

    class PipelinedRenderer:
        def render_and_defer_publish(self, store, source, chapter, **kwargs):
            events.append(("synth", chapter))
            if chapter == 2:
                ch2_synth_started.set()

            def finalize():
                if chapter == 1:
                    # Wait until chapter 2 has already started GPU synthesis!
                    assert ch2_synth_started.wait(timeout=5.0)
                    ch1_encode_gate.set()
                events.append(("publish", chapter))
                prefix = layout.audio_prefix(source["slug"], chapter)
                blob = b"OggS-fake"
                digest = sha256_bytes(blob)
                tag_man_sha = sha256_bytes(store.get_bytes(layout.tag_manifest_key(source["slug"], chapter)))
                spec = next(item for item in source["chapters"] if int(item["chapter"]) == int(chapter))
                publish_chapter_audio(
                    store,
                    slug=source["slug"],
                    chapter=chapter,
                    dest_prefix=prefix,
                    files={"title.opus": blob, "0.opus": blob, "chapter.opus": blob},
                    manifest={
                        "novel": source["slug"],
                        "chapter": chapter,
                        "source_sha256": spec["sha256"],
                        "tag_manifest_sha256": tag_man_sha,
                        "title": {"file": "title.opus", "bytes": len(blob), "sha256": digest},
                        "paragraphs": [{"file": "0.opus", "bytes": len(blob), "sha256": digest}],
                        "chapter_file": {"file": "chapter.opus", "bytes": len(blob), "sha256": digest},
                    },
                    attempt_id=f"pipe-{chapter}",
                )

            return finalize

    assert execute(args(tmp_path, stage="audio", validate=True), store, renderer=PipelinedRenderer(), inline=True) == EXIT_OK
    assert ch1_encode_gate.is_set()
    assert events.index(("synth", 2)) < events.index(("publish", 1))


def test_stage_all_streams_tagged_chapters_immediately_to_audio_workers(tmp_path):
    store = FakeStore()
    book = write_book(tmp_path, {1: "Chapter one.", 2: "Chapter two.", 3: "Chapter three."})
    publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=book, chapters=[1, 2, 3])

    timeline = []
    ch1_audio_started = threading.Event()

    class SlowAfterFirstTagger(FakeTagger):
        def tag_chapter(self, rows, batch_size, device):
            out = super().tag_chapter(rows, batch_size, device)
            timeline.append(("tagged_rows", len(self.batches)))
            if len(self.batches) >= 2:
                # Chapter 2 tagging waits until an audio worker has ALREADY started Chapter 1!
                assert ch1_audio_started.wait(timeout=5.0)
            return out

    class StreamingRenderer:
        def render_and_publish(self, store, source, chapter, *args, **kwargs):
            timeline.append(("audio_start", chapter))
            if chapter == 1:
                ch1_audio_started.set()
            prefix = layout.audio_prefix(source["slug"], chapter)
            blob = b"OggS-fake"
            digest = sha256_bytes(blob)
            tag_man_sha = sha256_bytes(store.get_bytes(layout.tag_manifest_key(source["slug"], chapter)))
            spec = next(item for item in source["chapters"] if int(item["chapter"]) == int(chapter))
            publish_chapter_audio(
                store,
                slug=source["slug"],
                chapter=chapter,
                dest_prefix=prefix,
                files={"title.opus": blob, "0.opus": blob, "chapter.opus": blob},
                manifest={
                    "novel": source["slug"],
                    "chapter": chapter,
                    "source_sha256": spec["sha256"],
                    "tag_manifest_sha256": tag_man_sha,
                    "title": {"file": "title.opus", "bytes": len(blob), "sha256": digest},
                    "paragraphs": [{"file": "0.opus", "bytes": len(blob), "sha256": digest}],
                    "chapter_file": {"file": "chapter.opus", "bytes": len(blob), "sha256": digest},
                },
                attempt_id=f"stream-{chapter}",
            )

    tagger = SlowAfterFirstTagger()
    renderer = StreamingRenderer()
    code = execute(
        args(tmp_path, stage="all", workers=3, tag_batch_size=16, audio_batch_size=8, validate=True),
        store,
        tagger=tagger,
        renderer=renderer,
        inline=True,
    )
    assert code == EXIT_OK
    assert ch1_audio_started.is_set()
    assert timeline.index(("audio_start", 1)) < timeline.index(("tagged_rows", 3))


def test_validation_hash_mismatch_and_publish_order(tmp_path):
    store = FakeStore()
    book = write_book(tmp_path, {1: "Hello."})
    publish_novel_tree(store, name="The Eye of the World", slug="the-eye-of-the-world", source_dir=book, chapters=[1])
    tagger = FakeTagger()
    assert execute(args(tmp_path, stage="tag", batch_size=16, validate=True), store, tagger=tagger, inline=True) == EXIT_OK
    puts = list(store.puts)
    manifest_at = max(index for index, key in enumerate(puts) if key.endswith("chapter_00001.manifest.json"))
    blob_at = max(index for index, key in enumerate(puts) if "/blobs/" in key)
    assert blob_at < manifest_at
    prefix = layout.audio_prefix("the-eye-of-the-world", 1)
    publish_chapter_audio(
        store, slug="the-eye-of-the-world", chapter=1, dest_prefix=prefix,
        files={"title.opus": b"aaa", "chapter.opus": b"bbb"},
        manifest={"novel": "the-eye-of-the-world", "chapter": 1, "title": {"file": "title.opus"}, "paragraphs": [], "chapter_file": {"file": "chapter.opus"}, "source_sha256": "x"},
        attempt_id="attempt-a",
    )
    assert store.puts.index(prefix + "manifest.json") > store.puts.index(prefix + "title.opus")
    with pytest.raises(ConflictError):
        publish_chapter_audio(
            store, slug="the-eye-of-the-world", chapter=1, dest_prefix=prefix,
            files={"title.opus": b"zzz", "chapter.opus": b"yyy"},
            manifest={"novel": "the-eye-of-the-world", "chapter": 1, "title": {"file": "title.opus"}, "paragraphs": [], "chapter_file": {"file": "chapter.opus"}, "source_sha256": "x"},
            attempt_id="attempt-b",
        )
    assert store.get_bytes(prefix + "title.opus") == b"aaa"
    assert execute(args(tmp_path, command="validate", stage="audio"), store, inline=True) == EXIT_INVALID


def test_batch_defaults_device_and_hash(tmp_path):
    assert resolve_batch("tag", "mps", batch_size=None, tag_batch_size=None, audio_batch_size=None, profile={}, model_key="m")[0] == 32
    assert resolve_batch("tag", "cuda", batch_size=None, tag_batch_size=None, audio_batch_size=None, profile={}, model_key="m")[0] == 64
    assert resolve_batch("audio", "mps", batch_size=None, tag_batch_size=None, audio_batch_size=None, profile={}, model_key="m")[0] == 4
    assert resolve_batch("audio", "cuda", batch_size=None, tag_batch_size=None, audio_batch_size=None, profile={}, model_key="m")[0] == 16
    assert resolve_batch("audio", "cpu", batch_size=None, tag_batch_size=None, audio_batch_size=None, profile={}, model_key="m")[0] == 1
    assert resolve_batch("audio", "cuda", batch_size=4, tag_batch_size=None, audio_batch_size=None, profile={}, model_key="m")[0] == 4
    assert resolve_batch("tag", "cpu", batch_size=8, tag_batch_size=3, audio_batch_size=None, profile={}, model_key="m")[0] == 3
    seen = []

    def boom(batch):
        seen.append(batch)
        if batch > 1:
            raise RuntimeError("mps backend out of memory")
        return "ok"

    result, effective, tried = call_reducing(8, boom)
    assert (result, effective, tried) == ("ok", 1, [8, 4, 2, 1])
    assert group_batches([10, 10, 3], 4, 25)[0] == [0, 1]
    cells = {"narrator/neutral": [0], "narrator/anger": [1]}
    entries = [{"index": 0, "cell": "narrator/neutral"}, {"index": 1, "cell": "narrator/anger"}]
    assert pick_style(cells, entries, "narrator", "awe", ("book", 1))["fallback"] is True
    assert pick_style(cells, entries, "narrator", "anger", ("book", 1))["fallback"] is False

    class Cuda:
        def is_available(self):
            return True

    class Backends:
        class mps:
            @staticmethod
            def is_available():
                return False

    class Torch:
        cuda = Cuda()
        backends = Backends()

    assert select_device("auto", Torch()) == "cuda"
    assert select_device("cpu", Torch()) == "cpu"
    partial = tmp_path / "blob.partial"
    partial.write_bytes(b"abc")
    with pytest.raises(HashMismatch):
        commit_download(partial, tmp_path / "blob", "deadbeef", 3)
    assert not partial.exists()


def test_namespace_keys_do_not_look_like_production(monkeypatch):
    monkeypatch.setenv("AUDIOBOOK_WORKER_NAMESPACE", "student_tts/_smoke/unit")
    key = layout.audio_prefix("the-eye-of-the-world", 1)
    assert key.startswith("student_tts/_smoke/unit/")
    monkeypatch.delenv("AUDIOBOOK_WORKER_NAMESPACE")
    assert layout.audio_prefix("the-eye-of-the-world", 1) == "audio-student/styletts2-eye-v1/the-eye-of-the-world/chapter_1/"
