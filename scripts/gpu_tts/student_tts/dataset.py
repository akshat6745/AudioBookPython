"""Build a deterministic, emotion-balanced Higgs request manifest."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import higgs_format as hf

VOICES = ("narrator", "dialogue")
EMOTIONS = (
    "neutral", "anger", "fear", "sadness", "amusement", "contemplation",
    "surprise", "determination", "bitterness", "affection",
)
# At ~8 seconds/clip: 2,700 clips/voice = 6 h/voice = 12 h total.
PER_VOICE_QUOTAS = {"neutral": 900, **{e: 200 for e in EMOTIONS if e != "neutral"}}
MIN_S, MAX_S, MAX_CHARS = 1.0, 16.0, 180


def estimated_seconds(text: str) -> float:
    return round(min(MAX_S, max(MIN_S, len(hf.plain(text)) / 15)), 2)


def _rank(row: dict) -> str:
    identity = "|".join(str(row.get(k, "")) for k in ("novel", "chapter", "paragraph", "voice", "text"))
    return hashlib.sha256(identity.encode()).hexdigest()


def candidates(source_rows: list[dict]) -> list[dict]:
    """Expand tagged paragraphs into 1-16 second, voice-specific requests."""
    out = []
    for row in source_rows:
        tag = row.get("tag", row)
        for voice, units in hf.format_dual(row["text"], tag):
            for chunk_no, tagged in enumerate(hf.chunks(units, max_chars=MAX_CHARS)):
                found = hf.TAG_RE.search(tagged)
                emotion = found.group(2) if found and found.group(1) == "emotion" else "neutral"
                if emotion not in EMOTIONS:
                    continue
                item = {
                    "novel": row["novel"], "chapter": int(row["chapter"]),
                    "paragraph": int(row["paragraph"]), "chunk": chunk_no,
                    "voice": voice, "emotion": emotion, "text": hf.plain(tagged),
                    "tagged_text": tagged, "estimated_s": estimated_seconds(tagged),
                }
                if MIN_S <= item["estimated_s"] <= MAX_S:
                    out.append(item)
    return out


def select_balanced(rows: list[dict], quotas: dict[str, int] | None = None) -> list[dict]:
    quotas = quotas or PER_VOICE_QUOTAS
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["voice"], row["emotion"])].append(row)
    selected = []
    for voice in VOICES:
        for emotion, count in quotas.items():
            # Center every stratum near eight seconds so clip quotas also control
            # hours instead of allowing one emotion to dominate by duration.
            pool = sorted(grouped[(voice, emotion)],
                          key=lambda row: (abs(row["estimated_s"] - 8.0), _rank(row)))
            if len(pool) < count:
                raise ValueError(f"insufficient {voice}/{emotion}: need {count}, found {len(pool)}")
            selected.extend(pool[:count])
    return sorted(selected, key=_rank)


def summary(rows: list[dict]) -> dict:
    seconds = Counter()
    counts = Counter()
    for row in rows:
        key = f"{row['voice']}/{row['emotion']}"
        counts[key] += 1
        seconds[row["voice"]] += row["estimated_s"]
    total = sum(seconds.values())
    neutral = sum(r["estimated_s"] for r in rows if r["emotion"] == "neutral")
    return {
        "clips": len(rows), "counts": dict(sorted(counts.items())),
        "estimated_hours": {k: round(v / 3600, 2) for k, v in seconds.items()},
        "total_estimated_hours": round(total / 3600, 2),
        "neutral_audio_pct": round(100 * neutral / total, 1) if total else 0,
    }


def write_manifest(path: Path, rows: list[dict]) -> dict:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    report = summary(rows)
    path.with_suffix(".summary.json").write_text(json.dumps(report, indent=2))
    return report
