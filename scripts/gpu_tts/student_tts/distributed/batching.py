"""Batch size selection and out-of-memory fallback. Batch size is not worker count."""

from __future__ import annotations

from .layout import AUDIO_CPU_DEFAULT_BATCH, AUDIO_DEFAULT_BATCH, TAG_DEFAULT_BATCH


def is_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "out of memory" in text or "mps backend out of memory" in text


def next_batch(current: int) -> int | None:
    if current <= 1:
        return None
    return max(1, current // 2)


def stage_default(stage: str, device: str) -> int:
    if stage == "tag":
        return TAG_DEFAULT_BATCH
    if device == "cpu":
        return AUDIO_CPU_DEFAULT_BATCH
    return AUDIO_DEFAULT_BATCH


def resolve_batch(
    stage: str,
    device: str,
    *,
    batch_size: int | None,
    tag_batch_size: int | None,
    audio_batch_size: int | None,
    profile: dict | None,
    model_key: str,
    explicit: bool | None = None,
) -> tuple[int, str]:
    """Return (batch, reason).

    ``--tag-batch-size`` / ``--audio-batch-size`` win when set, so ``--stage all`` can
    size each stage. Otherwise ``--batch-size`` overrides that stage's default.
    A saved machine profile only lowers the default; an explicit flag is not reduced
    until an OOM fallback records a new value.
    """
    specific = tag_batch_size if stage == "tag" else audio_batch_size
    if specific is not None:
        if specific < 1:
            raise ValueError("batch size must be >= 1")
        return specific, "stage-flag"
    if batch_size is not None:
        if batch_size < 1:
            raise ValueError("batch size must be >= 1")
        return batch_size, "global-flag"
    requested = stage_default(stage, device)
    saved = None
    if profile:
        saved = profile.get(stage, {}).get(device, {}).get(model_key)
    if isinstance(saved, int) and 1 <= saved < requested:
        return saved, "machine-profile"
    return requested, "default"


def call_reducing(start: int, fn, on_reduce=None):
    """Call ``fn(batch)``. On OOM, retry at floor(batch/2) down to 1. Never raises the batch."""
    if start < 1:
        raise ValueError("batch size must be >= 1")
    batch = start
    tried: list[int] = []
    while True:
        tried.append(batch)
        try:
            return fn(batch), batch, tried
        except RuntimeError as exc:
            nxt = next_batch(batch) if is_oom(exc) else None
            if nxt is None:
                raise
            batch = nxt
            if on_reduce is not None:
                on_reduce(batch, exc)


def group_batches(lengths: list[int], batch: int, token_budget: int) -> list[list[int]]:
    """Group longest-first, matching the student renderer. ``token_budget`` caps batch * width."""
    if batch < 1:
        raise ValueError("batch size must be >= 1")
    order = sorted(range(len(lengths)), key=lambda index: -lengths[index])
    groups: list[list[int]] = []
    current: list[int] = []
    for index in order:
        width = lengths[current[0]] if current else lengths[index]
        if current and (len(current) == batch or (len(current) + 1) * width > token_budget):
            groups.append(current)
            current = []
        current.append(index)
    if current:
        groups.append(current)
    return groups
