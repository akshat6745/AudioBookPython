"""Shared label set + local SQLite store for the emotion-detector bake-off."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DATA = Path(os.environ.get("SANDBOX_WORK") or Path(__file__).resolve().parent / "data")
DB_PATH = DATA / "emotion_eval.db"

# Higgs v3 emotion tags (PROMPTING.md) minus "arousal", plus neutral = no tag.
EMOTIONS = ["neutral", "affection", "amusement", "anger", "awe", "bitterness", "confusion", "contemplation",
            "contentment", "determination", "disgust", "elation", "enthusiasm", "fear", "helplessness",
            "longing", "pride", "relief", "sadness", "shame", "surprise"]
STYLES = ["none", "whispering", "shouting"]
SFX = ["none", "laughter", "sigh", "crying", "sniff", "screaming", "cough"]

# Coarse 7-way groups so small classifiers (fewer labels) can be compared fairly.
COARSE = {
    "neutral": "neutral",
    **dict.fromkeys(["affection", "amusement", "awe", "contentment", "elation", "enthusiasm", "pride", "relief"], "positive"),
    **dict.fromkeys(["sadness", "longing", "helplessness", "shame"], "sad"),
    **dict.fromkeys(["anger", "bitterness", "disgust"], "angry"),
    "fear": "fear",
    **dict.fromkeys(["surprise", "confusion"], "surprise"),
    **dict.fromkeys(["contemplation", "determination"], "resolve"),
}
COARSE_LABELS = ["neutral", "positive", "sad", "angry", "fear", "surprise", "resolve"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id             TEXT PRIMARY KEY,          -- e.g. lord-of-the-mysteries_ch4_s012
    novel_id       TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    segment_index  INTEGER NOT NULL,
    segment_type   TEXT NOT NULL CHECK(segment_type IN ('narration','dialogue')),
    text           TEXT NOT NULL,
    UNIQUE(novel_id, chapter_number, segment_index)
);
CREATE TABLE IF NOT EXISTS labels (
    segment_id  TEXT NOT NULL REFERENCES segments(id),
    source      TEXT NOT NULL,                -- model id or 'reference'
    emotion     TEXT,                         -- fine Higgs label (NULL if the model only has coarse labels)
    coarse      TEXT NOT NULL,
    style       TEXT DEFAULT 'none',
    sfx         TEXT DEFAULT 'none',
    intensity   REAL,
    raw         TEXT,                         -- raw model output for debugging
    created_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (segment_id, source)
);
CREATE TABLE IF NOT EXISTS runs (
    source          TEXT PRIMARY KEY,
    model           TEXT NOT NULL,
    params          TEXT,
    seconds         REAL,
    segments        INTEGER,
    parse_failures  INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def save_labels(con: sqlite3.Connection, source: str, rows: list[dict]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO labels (segment_id, source, emotion, coarse, style, sfx, intensity, raw) "
        "VALUES (:segment_id, :source, :emotion, :coarse, :style, :sfx, :intensity, :raw)",
        [{"source": source, "emotion": None, "style": "none", "sfx": "none", "intensity": None, "raw": None, **r}
         for r in rows])
    con.commit()


def segments(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM segments ORDER BY novel_id, chapter_number, segment_index").fetchall()
