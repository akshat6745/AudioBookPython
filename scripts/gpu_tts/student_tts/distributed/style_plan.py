"""Style-bank selection and paragraph planning. Higgs helpers load only when planning audio."""

from __future__ import annotations

import hashlib
from pathlib import Path

BANK_EMOTIONS = (
    "neutral", "affection", "amusement", "anger", "bitterness",
    "contemplation", "determination", "fear", "sadness", "surprise",
)
MAX_CHARS = 300
PARA_GAP_S = 0.35
CHUNK_GAP_S = 0.12
BREAK_GAP_S = 1.0
SKIP_S = 0.3


def seed_for(*parts) -> int:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def pick_style(cells: dict[str, list[int]], entries: list[dict], voice: str, emotion: str, key: tuple) -> dict:
    """Missing emotions fall back to ``voice/neutral``. Other labels are not invented."""
    cell = f"{voice}/{emotion}"
    fallback = cell not in cells
    if fallback:
        cell = f"{voice}/neutral"
    if cell not in cells:
        raise KeyError(f"style bank has no {cell}")
    indexes = cells[cell]
    entry = entries[indexes[seed_for(*key, voice, cell) % len(indexes)]]
    return {**entry, "requested": f"{voice}/{emotion}", "fallback": fallback}


class StyleBank:
    def __init__(self, npz_path: Path, json_path: Path):
        import json

        import numpy as np

        meta = json.loads(json_path.read_text())
        self.vectors = np.load(npz_path)["ref_s"]
        self.entries = meta["entries"]
        self.cells: dict[str, list[int]] = {}
        for entry in self.entries:
            self.cells.setdefault(entry["cell"], []).append(entry["index"])

    def pick(self, voice: str, emotion: str, *key) -> dict:
        return pick_style(self.cells, self.entries, voice, emotion, key)


def _higgs():
    import sys

    root = Path(__file__).resolve().parents[2] / "audiobook"
    path = str(root)
    if path not in sys.path:
        sys.path.insert(0, path)
    import higgs_format as hf

    return hf


def plan_paragraph(paragraph: str, tag: dict) -> list[dict]:
    """Same narrator/dialogue split as bank_tts.plan_paragraph. Watermarks yield no speech."""
    hf = _higgs()
    if hf.is_watermark(paragraph):
        return []
    out = []
    for voice, units in hf.format_dual(paragraph, tag, hf.Cuts()):
        for chunk in hf.chunks(units, MAX_CHARS):
            import re

            match = re.match(r"<\|emotion:([a-z_]+)\|>", chunk)
            text = hf.plain(chunk).strip()
            if text:
                out.append({"voice": voice, "emotion": match.group(1) if match else "neutral", "text": text})
    return out


def silence_seconds(paragraph: str) -> float:
    hf = _higgs()
    return SKIP_S if hf.is_watermark(paragraph) else BREAK_GAP_S


def split_long(text: str) -> list[str]:
    import re

    midpoint = len(text) / 2
    cuts = [match.end() for match in re.finditer(r"[;,—:]\s*", text) if 0 < match.end() < len(text)]
    if not cuts:
        raise ValueError("cannot split a long chunk without a clause break")
    cut = min(cuts, key=lambda point: abs(point - midpoint))
    return [text[:cut].rstrip(), text[cut:].lstrip()]
