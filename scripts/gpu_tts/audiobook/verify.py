"""Whisper check for one generated clip: was every book word spoken, and nothing else?

Higgs v3 occasionally (~1 clip in 40) skips the rest of a passage or keeps talking gibberish after the text ends;
a new seed almost always fixes it (outputs/realtime/qa). render.speak regenerates clips this check rejects.
Faults are runs of missing or extra words, not word error rate, so a misheard name ("Derek" for "Derrick") or a
number written as digits does not cost a retry.
"""
from __future__ import annotations

import difflib
import re

import check_audio as ca

MAX_RUN = 4  # this many missing or extra words in a row is a fault
SHORTEST, LONGEST = 0.4, 2.5  # clip length vs what the text needs (15 chars/s)
ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
       "seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_words(n: int) -> list[str]:
    if n < 20:
        return [ONES[n]]
    if n < 100:
        return [TENS[n // 10]] + ([ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [ONES[n // 100], "hundred"] + (number_words(n % 100) if n % 100 else [])
    if n < 10000:
        return number_words(n // 1000) + ["thousand"] + (number_words(n % 1000) if n % 1000 else [])
    return [str(n)]


def normalise(text: str) -> list[str]:
    out = []
    for w in ca.words(text):
        if ca.INTERJECTION.match(w) or w == "and":  # "one hundred and ten" vs "110"
            continue
        out.extend(number_words(int(w)) if w.isdigit() else [w])
    return out


def runs(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    """Longest stretch of book words not spoken, and of spoken words not in the book."""
    missing = extra = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            missing = max(missing, i2 - i1)
        if tag in ("insert", "replace"):
            extra = max(extra, j2 - j1)
    return missing, extra


def check_clip(text: str, audio, sr: int, transcribe) -> dict:
    """text: what the clip should say (tags removed). Returns ok, reasons, score (lower is better) and details."""
    ref = normalise(text)
    heard = transcribe(audio, sr).strip() if len(audio) else ""
    hyp = normalise(heard)
    missing, extra = runs(ref, hyp)
    ratio = len(audio) / sr / max(1.0, len(text) / 15)
    reasons = [r for r, bad in (
        ("skipped words", missing >= MAX_RUN),
        ("words not in the text", extra >= MAX_RUN),
        ("too long", ratio > LONGEST),
        ("too short", len(ref) >= 6 and ratio < SHORTEST),
        ("tag spoken", bool(ca.TAG_WORDS & set(hyp) - set(ref))),
    ) if bad]
    return {"ok": not reasons, "reasons": reasons, "score": missing + extra + 10 * len(reasons),
            "missing_run": missing, "extra_run": extra, "length_ratio": round(ratio, 2),
            "wer": round(ca.wer(ref, hyp), 3), "heard": heard}
