"""Chapter range parsing. The source manifest is the authority for legal chapters."""

from __future__ import annotations


class ChapterRangeError(Exception):
    def __init__(self, rejected: list[int], allowed: list[int]):
        self.rejected = rejected
        self.allowed = allowed
        super().__init__(
            "chapters outside the registered source manifest: "
            + ",".join(str(c) for c in rejected)
            + ". Registered chapters: "
            + _compact(allowed)
        )


def _compact(chapters: list[int]) -> str:
    if not chapters:
        return "(none)"
    ordered = sorted(set(chapters))
    ranges: list[str] = []
    start = prev = ordered[0]
    for chapter in ordered[1:]:
        if chapter == prev + 1:
            prev = chapter
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = chapter
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)


def parse_chapter_spec(spec: str) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for part in spec.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            left, right = piece.split("-", 1)
            lo, hi = int(left), int(right)
            if hi < lo or lo < 1:
                raise ChapterRangeError([lo, hi], [])
            block = range(lo, hi + 1)
        else:
            value = int(piece)
            if value < 1:
                raise ChapterRangeError([value], [])
            block = (value,)
        for chapter in block:
            if chapter not in seen:
                seen.add(chapter)
                out.append(chapter)
    if not out:
        raise ChapterRangeError([], [])
    return out


def select_chapters(spec: str | None, allowed: list[int]) -> list[int]:
    if spec is None or not str(spec).strip():
        return list(allowed)
    chosen = parse_chapter_spec(spec)
    allowed_set = set(allowed)
    rejected = [chapter for chapter in chosen if chapter not in allowed_set]
    if rejected:
        raise ChapterRangeError(rejected, allowed)
    return chosen


def pending_chapters(selected: list[int], finished: set[int]) -> list[int]:
    """Every selected chapter that is not finished, including gaps. Not only a contiguous prefix."""
    return [chapter for chapter in selected if chapter not in finished]
