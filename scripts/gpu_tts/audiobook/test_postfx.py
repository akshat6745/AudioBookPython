"""pytest scripts/gpu_tts/audiobook/test_postfx.py"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import postfx  # noqa: E402

SR = 24000
rng = np.random.default_rng(0)


def tone(s, amp=0.1):
    t = np.arange(int(s * SR)) / SR
    return (amp * np.sin(2 * np.pi * 180 * t) * (1 + 0.3 * np.sin(2 * np.pi * 4 * t))).astype(np.float32)


def quiet(s):
    return (rng.normal(0, 1e-5, int(s * SR))).astype(np.float32)


def test_drops_loud_burst_after_pause():
    audio = np.concatenate([quiet(0.2), tone(1.5), quiet(0.25), (rng.normal(0, 0.3, int(0.15 * SR))).astype(np.float32)])
    out, info = postfx.tidy(audio, SR)
    assert 0.1 <= info["dropped_burst_s"] <= 0.2
    assert len(out) / SR < 1.5 + 2 * postfx.EDGE_S + 0.02


def test_keeps_short_final_word_at_normal_level():
    audio = np.concatenate([tone(1.0), quiet(0.25), tone(0.25), quiet(0.3)])  # "Uh, ... no."
    out, info = postfx.tidy(audio, SR)
    assert info["dropped_burst_s"] == 0
    assert len(out) / SR > 1.45


def test_keeps_audio_without_pause():
    audio = np.concatenate([tone(1.0), tone(0.2, amp=0.4)])  # loud end, but no pause before it
    _, info = postfx.tidy(audio, SR)
    assert info["dropped_burst_s"] == 0


def test_trims_silence_and_fades_edges():
    audio = np.concatenate([quiet(0.5), tone(1.0), quiet(0.6)])
    out, _ = postfx.tidy(audio, SR)
    assert abs(len(out) / SR - (1.0 + 2 * postfx.EDGE_S)) < 0.03
    assert out[0] == 0 and out[-1] == 0


def test_match_level_equalises_voices():
    a = postfx.match_level(np.concatenate([tone(1, 0.05), quiet(0.3)]), SR)
    b = postfx.match_level(np.concatenate([tone(1, 0.4), quiet(0.3)]), SR)
    assert abs(np.sqrt(np.mean(a[:SR] ** 2)) / np.sqrt(np.mean(b[:SR] ** 2)) - 1) < 0.05


def test_drops_quiet_noise_running_into_the_end():
    # Measured shape: speech, ~0.4 s digital silence, then 80-160 ms of noise quieter than the speech, up to the end.
    audio = np.concatenate([tone(1.2, 0.2), np.zeros(int(0.4 * SR), np.float32),
                            (rng.normal(0, 0.05, int(0.12 * SR))).astype(np.float32)])
    out, info = postfx.tidy(audio, SR)
    assert 0.1 <= info["dropped_burst_s"] <= 0.14
    assert len(out) / SR < 1.2 + 2 * postfx.EDGE_S + 0.02


def test_keeps_final_word_followed_by_silence():
    audio = np.concatenate([tone(1.0), quiet(0.25), tone(0.2), np.zeros(int(0.3 * SR), np.float32)])
    _, info = postfx.tidy(audio, SR)
    assert info["dropped_burst_s"] == 0


def test_drops_end_noise_in_several_pieces():
    noise = lambda s, a: (rng.normal(0, a, int(s * SR))).astype(np.float32)  # noqa: E731
    audio = np.concatenate([tone(1.2, 0.2), np.zeros(int(0.35 * SR), np.float32), noise(0.03, 0.01),
                            np.zeros(int(0.05 * SR), np.float32), noise(0.1, 0.1)])
    out, info = postfx.tidy(audio, SR)
    assert info["dropped_burst_s"] > 0.1
    assert len(out) / SR < 1.2 + 2 * postfx.EDGE_S + 0.02


def test_keeps_final_consonant_release():
    # "...sleep.": closure (~80 ms silence) then a ~100 ms release, quieter than the vowel. Speech, not noise.
    audio = np.concatenate([tone(1.2, 0.2), np.zeros(int(0.08 * SR), np.float32),
                            (rng.normal(0, 0.03, int(0.1 * SR))).astype(np.float32), np.zeros(int(0.05 * SR), np.float32)])
    _, info = postfx.tidy(audio, SR)
    assert info["dropped_burst_s"] == 0


def test_keeps_short_last_word_with_trailing_silence():
    audio = np.concatenate([tone(1.0), np.zeros(int(0.08 * SR), np.float32), tone(0.15), np.zeros(int(0.3 * SR), np.float32)])
    _, info = postfx.tidy(audio, SR)
    assert info["dropped_burst_s"] == 0
