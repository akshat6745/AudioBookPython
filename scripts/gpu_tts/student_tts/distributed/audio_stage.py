"""Audio stage. Tag preflight runs before StyleTTS is imported."""

from __future__ import annotations

import os

from . import layout
from .batching import resolve_batch
from .exitcodes import EXIT_INCOMPLETE, EXIT_INVALID, EXIT_OK, code_for_statuses, merge_codes
from .leases import claim_chapter
from .validate import AUDIO_READY, VALID_TAG, classify_audio, classify_tag

REDO = frozenset({"missing", "stale", "leased"})
BLOCKED = frozenset({"conflict", "hash_mismatch", "invalid"})


def _load_stack():
    from . import audio_render

    return audio_render


def preflight_tags(store, source, chapters, *, now: float, allow_partial: bool) -> tuple[int | None, list[dict], str]:
    """Return (exit code or None, tag rows, summary). None means tagging is sufficient to continue."""
    from .validate import _pool_map

    workers = 16 if store.__class__.__name__ == "BotoStore" else 1
    rows = _pool_map(workers, lambda chapter: classify_tag(store, source, chapter, now=now), chapters)
    bad = [row for row in rows if row["status"] not in VALID_TAG]
    if not bad:
        return None, rows, ""
    summary = _tag_summary(bad)
    if not allow_partial:
        return code_for_statuses([row["status"] for row in bad]), rows, summary
    return None, rows, summary


def _tag_summary(rows: list[dict]) -> str:
    grouped: dict[str, list[int]] = {}
    for row in rows:
        grouped.setdefault(row["status"], []).append(int(row["chapter"]))
    parts = [f"{status}: {values}" for status, values in sorted(grouped.items())]
    return (
        "incomplete tag manifests; refusing to load StyleTTS. "
        + "; ".join(parts)
        + '. Finish tagging first: audiobook-worker run --novel "<name>" --stage tag --validate'
    )


def chapters_to_render(store, source, chapters, tag_rows, *, now: float, allow_partial: bool, replace_stale: bool = False) -> tuple[list[int], list[dict]]:
    from .validate import _pool_map

    by_chapter = {int(row["chapter"]): row for row in tag_rows}
    work = []
    ready = {"valid"} if replace_stale else AUDIO_READY
    redo = REDO | ({"legacy_valid"} if replace_stale else set())
    workers = 16 if store.__class__.__name__ == "BotoStore" else 1
    audio_rows = _pool_map(
        workers,
        lambda chapter: classify_audio(store, source, chapter, now=now, tag_row=by_chapter[chapter]),
        chapters,
    )
    for chapter, audio_row in zip(chapters, audio_rows):
        tag_row = by_chapter[chapter]
        if audio_row["status"] in ready:
            continue
        if tag_row["status"] not in VALID_TAG:
            if allow_partial:
                continue
            continue
        if audio_row["status"] in redo:
            work.append(chapter)
    return work, audio_rows


def worker_loop(store, source, chapters, *, identity: dict, batch: int, device: str, now_fn, log, renderer=None, profile_file=None, ttl: float | None = None, token_budget: int = layout.BATCH_TOKENS, replace_stale: bool = False) -> int:
    from concurrent.futures import Future, ThreadPoolExecutor

    owner = identity["owner"]
    codes = []
    ready = {"valid"} if replace_stale else AUDIO_READY
    redo = REDO | ({"legacy_valid"} if replace_stale else set())
    in_flight_chapters: set[int] = set()
    pending_publish: Future | None = None
    bg_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-publish")

    def _run_background_finalize(finalize_fn, claimed_lease, hb_ctx, ch_num: int) -> None:
        try:
            finalize_fn()
        finally:
            try:
                hb_ctx.__exit__(None, None, None)
            finally:
                claimed_lease.release(now_fn())
                in_flight_chapters.discard(ch_num)

    try:
        while True:
            if pending_publish is not None and pending_publish.done():
                pending_publish.result()
                pending_publish = None
            claimed = None
            target = None
            now = now_fn()
            for chapter in chapters:
                if chapter in in_flight_chapters:
                    continue
                tag_row = classify_tag(store, source, chapter, now=now)
                if tag_row["status"] not in VALID_TAG:
                    continue
                audio_row = classify_audio(store, source, chapter, now=now, tag_row=tag_row)
                if audio_row["status"] in ready:
                    continue
                if audio_row["status"] in BLOCKED:
                    codes.append(EXIT_INVALID)
                    continue
                if audio_row["status"] not in redo:
                    continue
                kwargs = dict(
                    store=store, slug=source["slug"], stage="audio", chapter=chapter, owner=owner,
                    run_id=identity["run_id"], worker_index=identity["worker_index"],
                    runtime_version=layout.STYLETTS_VERSION, now=now, host=identity["host"], pid=os.getpid(),
                )
                if ttl is not None:
                    kwargs["ttl"] = ttl
                claimed = claim_chapter(**kwargs)
                if claimed is None:
                    continue
                again = classify_audio(store, source, chapter, now=now_fn(), tag_row=tag_row)
                if again["status"] not in redo:
                    claimed.release(now_fn())
                    claimed = None
                    continue
                target = chapter
                break
            if target is None:
                break
            assert claimed is not None
            from .leases import Heartbeat

            hb = Heartbeat(claimed, now_fn)
            hb.__enter__()
            handed_off = False
            try:
                if renderer is None:
                    renderer = _load_stack()
                r_kwargs = dict(
                    batch=batch, device=device, worker_id=identity["worker_id"],
                    profile_file=profile_file, token_budget=token_budget, log=log, claim=claimed, now_fn=now_fn,
                )
                if replace_stale:
                    r_kwargs["replace_stale"] = True
                if hasattr(renderer, "render_and_defer_publish"):
                    finalize = renderer.render_and_defer_publish(store, source, target, **r_kwargs)
                    if finalize is not None:
                        if pending_publish is not None:
                            pending_publish.result()
                            pending_publish = None
                        in_flight_chapters.add(target)
                        pending_publish = bg_pool.submit(_run_background_finalize, finalize, claimed, hb, target)
                        handed_off = True
                else:
                    renderer.render_and_publish(store, source, target, **r_kwargs)
                codes.append(EXIT_OK)
            except Exception:
                codes.append(EXIT_INVALID)
                raise
            finally:
                if not handed_off:
                    try:
                        hb.__exit__(None, None, None)
                    finally:
                        claimed.release(now_fn())
        if pending_publish is not None:
            pending_publish.result()
            pending_publish = None
    except Exception:
        codes.append(EXIT_INVALID)
        raise
    finally:
        bg_pool.shutdown(wait=True)
    if any(code == EXIT_INVALID for code in codes):
        return EXIT_INVALID
    return merge_codes(*codes) if codes else EXIT_OK


def audio_exit_if_blocked(summary: str, code: int) -> str:
    if code == EXIT_INCOMPLETE:
        return summary
    return summary
