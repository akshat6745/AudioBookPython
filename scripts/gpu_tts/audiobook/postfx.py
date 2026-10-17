"""Clean up one Higgs request before stitching: drop the stray end-of-request burst, trim silence, fade, level-match.

Higgs's last 2-4 codec frames (80-160 ms, just before it emits end-of-audio) are often noise after the speech has
already ended in silence. It is in the generated codes, not the decoder or stitching, and is the "loud end of paragraph".
"""
from __future__ import annotations

import numpy as np

FRAME_S = 0.01
SILENT_DB = -40.0  # frame counts as silence this far below the request's speech level
BURST_MAX_S = 0.3  # a final sound island this short, after a pause, is a candidate artifact
BURST_GAP_S = 0.12
BURST_LOUDER_DB = 4.0  # ...and it is only dropped if it is this much louder than the speech before it
TAIL_MAX_S = 0.3  # whatever follows the last silence, if this short, is end-of-audio noise
TAIL_GAP_S = 0.15  # a real last word is always followed by its own silence, so it is never after this one
EDGE_S = 0.03  # silence kept before the first and after the last speech frame
FADE_IN_S, FADE_OUT_S = 0.005, 0.02
TARGET_DB = -20.0  # RMS of speech frames; chapter loudness is finished by ffmpeg loudnorm


def _frames_db(audio: np.ndarray, n: int) -> np.ndarray:
    k = len(audio) // n
    rms = np.sqrt(np.mean(audio[:k * n].reshape(k, n) ** 2, axis=1))
    return 20 * np.log10(rms + 1e-9)


def tidy(audio: np.ndarray, sr: int) -> tuple[np.ndarray, dict]:
    n = int(FRAME_S * sr)
    db = _frames_db(audio, n)
    info = {"dropped_burst_s": 0.0}
    if len(db) < 5:
        return audio, info
    level = np.percentile(db, 90)
    voiced = db > level + SILENT_DB
    idx = np.nonzero(voiced)[0]
    if not len(idx):
        return audio, info

    breaks = np.nonzero(np.diff(idx) > 1)[0]
    # A short gap then a short sound at the end is usually a final p/t/k release ("sleep.", "look."): keep it.
    # End-of-audio noise (may come in several pieces): everything after the last long silence, if it is short.
    long_gaps = [b for b in breaks if (idx[b + 1] - idx[b] - 1) * FRAME_S >= TAIL_GAP_S]
    if long_gaps and (len(db) - idx[long_gaps[-1] + 1]) * FRAME_S <= TAIL_MAX_S:
        b = long_gaps[-1]
        info["dropped_burst_s"] = round(info["dropped_burst_s"] + (idx[-1] + 1 - idx[b + 1]) * FRAME_S, 2)
        idx = idx[:b + 1]
        breaks = breaks[breaks < b]
    # A loud burst after a shorter pause.
    if len(breaks):
        start, prev_end = idx[breaks[-1] + 1], idx[breaks[-1]]
        island, gap = (idx[-1] + 1 - start) * FRAME_S, (start - prev_end - 1) * FRAME_S
        before = db[max(0, prev_end - 50):prev_end + 1]
        before = before[before > level + SILENT_DB]
        if (island <= BURST_MAX_S and gap >= BURST_GAP_S and len(before)
                and db[start:idx[-1] + 1].max() > before.max() + BURST_LOUDER_DB):
            info["dropped_burst_s"] = round(info["dropped_burst_s"] + island, 2)
            idx = idx[idx <= prev_end]

    lo = max(0, (idx[0] * n) - int(EDGE_S * sr))
    hi = min(len(audio), (idx[-1] + 1) * n + int(EDGE_S * sr))
    out = audio[lo:hi].astype(np.float32, copy=True)
    fi, fo = min(int(FADE_IN_S * sr), len(out)), min(int(FADE_OUT_S * sr), len(out))
    out[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
    out[len(out) - fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
    return out, info


def match_level(audio: np.ndarray, sr: int, target_db: float = TARGET_DB) -> np.ndarray:
    """Scale so speech frames sit at target_db, so both voices and every request play at the same loudness."""
    db = _frames_db(audio, int(FRAME_S * sr))
    if not len(db):
        return audio
    speech = db[db > np.percentile(db, 90) + SILENT_DB]
    gain = 10 ** ((target_db - 10 * np.log10(np.mean(10 ** (speech / 10)))) / 20)
    return (audio * gain).astype(np.float32)
