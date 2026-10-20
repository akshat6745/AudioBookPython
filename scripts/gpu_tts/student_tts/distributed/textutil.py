"""Paragraph splitting shared by tagging and rendering."""

from __future__ import annotations

import hashlib
import re


def raw_paragraphs(text: str) -> list[str]:
    """Same split as the app and render.paragraphs: blank-line paragraphs, stripped, empties dropped."""
    return [part.strip() for part in text.split("\n\n") if part.strip()]


def collapsed_paragraphs(text: str) -> list[str]:
    """Same split as tag_novel.novel_rows: internal whitespace collapsed to one space."""
    collapsed = [re.sub(r"\s+", " ", part).strip() for part in text.split("\n\n")]
    return [part for part in collapsed if part]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def tag_rows_for_chapter(text: str) -> list[dict]:
    """Rows consumed by the ensemble: collapsed text plus the previous paragraph as context."""
    paragraphs = collapsed_paragraphs(text)
    return [
        {"paragraph": index, "text": paragraph, "prev": paragraphs[index - 1] if index else ""}
        for index, paragraph in enumerate(paragraphs)
    ]
