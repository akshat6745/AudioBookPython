"""Stage orchestration. Model modules are imported only when that stage has work."""

from __future__ import annotations

import socket
import time
import uuid
from pathlib import Path

from .audio_stage import chapters_to_render, preflight_tags, worker_loop as audio_loop
from .batching import resolve_batch
from .chapters import ChapterRangeError, select_chapters
from .deviceutil import select_device
from .exitcodes import EXIT_INCOMPLETE, EXIT_INVALID, EXIT_OK, EXIT_SETUP, merge_codes
from .profile import load_profile
from .registry import RegistryError, load_index, resolve_novel
from .report import build_report, format_summary, write_report
from .tag_stage import worker_loop as tag_loop
from .validate import load_source, validate_stage, worst_code
from .worker import identity_for, run_processes, run_threads


def _now():
    return time.time()


def execute(args, store, *, now_fn=None, tagger=None, renderer=None, torch_mod=None, inline: bool | None = None) -> int:
    now_fn = now_fn or _now
    try:
        novel = resolve_novel(load_index(store), args.novel)
        source = load_source(store, novel["slug"])
        allowed = [int(row["chapter"]) for row in source["chapters"]]
        chapters = select_chapters(getattr(args, "chapters", None), allowed)
    except RegistryError as exc:
        print(str(exc))
        return EXIT_SETUP
    except FileNotFoundError as exc:
        print(str(exc))
        return EXIT_SETUP
    except ChapterRangeError as exc:
        print(str(exc))
        return EXIT_SETUP
    except (ValueError, KeyError) as exc:
        print(f"source manifest is invalid: {exc}")
        return EXIT_INVALID

    stage = args.stage
    if args.command == "validate" or getattr(args, "status_only", False):
        return _validate(args, store, novel, source, chapters, stage, now_fn)
    code = EXIT_OK
    if stage in {"tag", "all"}:
        code = merge_codes(code, _run_tag(args, store, source, chapters, now_fn, tagger, torch_mod, inline))
    if stage in {"audio", "all"} and code not in {EXIT_SETUP}:
        code = merge_codes(code, _run_audio(args, store, source, chapters, now_fn, renderer, torch_mod, inline))
    if getattr(args, "validate", False):
        code = merge_codes(code, _validate(args, store, novel, source, chapters, stage, now_fn))
    return code


def _validate(args, store, novel, source, chapters, stage, now_fn) -> int:
    rows = validate_stage(store, source, stage, chapters, now=now_fn(), workers=max(1, args.workers))
    report = build_report(novel=novel["name"], slug=novel["slug"], stage=stage, rows=rows, extra={
        "workers": args.workers,
        "command": "validate",
    })
    write_report(getattr(args, "report_json", None), report)
    print(format_summary(report))
    return worst_code(rows)


def _device(args, torch_mod):
    if getattr(args, "device", "auto") == "auto" and torch_mod is None:
        # Tests pass a fake module. Production imports torch inside select_device.
        try:
            return select_device(args.device, torch_mod)
        except Exception:
            return "cpu"
    return select_device(getattr(args, "device", "auto"), torch_mod)


def _batch(args, stage: str, device: str) -> int:
    profile = load_profile(Path(args.state) / "batch-profile.json") if getattr(args, "state", None) else load_profile()
    if getattr(args, "reset_batch_profile", False):
        profile = {}
    model_key = "emotion-tagger-v1@2026-10-20.1" if stage == "tag" else "styletts2-eye-v1@2026-10-20.1"
    batch, reason = resolve_batch(
        stage, device,
        batch_size=args.batch_size,
        tag_batch_size=getattr(args, "tag_batch_size", None),
        audio_batch_size=getattr(args, "audio_batch_size", None),
        profile=profile,
        model_key=model_key,
    )
    print(f"{stage} batch {batch} ({reason}) on {device}; workers {args.workers}")
    return batch


def _launch(stage, args, store, source, chapters, batch, device, now_fn, loop, extra, inline):
    run_id = getattr(args, "run_id", None) or uuid.uuid4().hex
    host = socket.gethostname()
    count = max(1, int(args.workers))
    if inline is False:
        payload = {
            "run_id": run_id,
            "host": host,
            "owners": [uuid.uuid4().hex for _ in range(count)],
            "source": source,
            "chapters": chapters,
            "batch": batch,
            "device": device,
            "namespace": __import__("os").environ.get("AUDIOBOOK_WORKER_NAMESPACE", ""),
        }
        return run_processes(stage, count, payload)
    calls = []
    state = getattr(args, "state", None)
    for index in range(count):
        identity = identity_for(run_id, index, host, uuid.uuid4().hex)

        def make(identity=identity):
            kwargs = dict(
                store=store, source=source, chapters=chapters, identity=identity,
                batch=batch, device=device, now_fn=now_fn,
                log=lambda message, identity=identity: print(f"[{identity['worker_id']}] {message}", flush=True),
                profile_file=(Path(state) / "batch-profile.json") if state else None,
            )
            kwargs.update(extra)
            return loop(**kwargs)

        calls.append((identity, make))
    if count == 1:
        return calls[0][1]()
    return run_threads(calls)


def _run_tag(args, store, source, chapters, now_fn, tagger, torch_mod, inline) -> int:
    device = _device(args, torch_mod)
    batch = _batch(args, "tag", device)
    extra = {}
    if tagger is not None:
        extra["tagger"] = tagger
    return _launch("tag", args, store, source, chapters, batch, device, now_fn, tag_loop, extra, inline)


def _run_audio(args, store, source, chapters, now_fn, renderer, torch_mod, inline) -> int:
    code, rows, summary = preflight_tags(
        store, source, chapters, now=now_fn(), allow_partial=bool(getattr(args, "allow_partial_tags", False)),
    )
    if summary:
        print(summary)
    if code is not None:
        report = build_report(novel=source.get("name", source["slug"]), slug=source["slug"], stage="audio", rows=rows, extra={"blocked": "tags"})
        write_report(getattr(args, "report_json", None), report)
        return code
    _work, audio_rows = chapters_to_render(
        store, source, chapters, rows, now=now_fn(), allow_partial=bool(getattr(args, "allow_partial_tags", False)),
    )
    blocked = [row for row in audio_rows if row["status"] in {"conflict", "hash_mismatch", "invalid"}]
    if blocked and not _work:
        report = build_report(novel=source.get("name", source["slug"]), slug=source["slug"], stage="audio", rows=audio_rows)
        write_report(getattr(args, "report_json", None), report)
        print(format_summary(report))
        return EXIT_INVALID
    if not _work:
        if any(row["status"] not in {"valid", "legacy_valid"} for row in rows):
            return EXIT_INCOMPLETE
        return EXIT_OK
    device = _device(args, torch_mod)
    batch = _batch(args, "audio", device)
    extra = {}
    if renderer is not None:
        extra["renderer"] = renderer
    # Only hand the worker chapters that are allowed to render.
    return _launch("audio", args, store, source, _work, batch, device, now_fn, audio_loop, extra, inline)
