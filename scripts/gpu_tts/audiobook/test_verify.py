"""verify.check_clip and render.speak's regenerate-until-it-passes loop, with fake audio and a fake Whisper."""
import hashlib
import json

import numpy as np
import pytest

import render
import verify

SR = 24000


def clip(text: str) -> np.ndarray:
    """Audio as long as the text needs at 15 chars/s, with speech-like level so postfx keeps it."""
    n = int(SR * max(1.0, len(text) / 15))
    return (0.3 * np.sin(np.linspace(0, 2000, n))).astype(np.float32)


@pytest.mark.parametrize("n,words", [(7, "seven"), (30, "thirty"), (110, "one hundred ten"),
                                     (1999, "one thousand nine hundred ninety nine")])
def test_number_words(n, words):
    assert verify.number_words(n) == words.split()


def test_numbers_and_misheard_names_pass():
    text = "The Overlook has one hundred and ten guest quarters, Derrick said."
    r = verify.check_clip(text, clip(text), SR, lambda a, sr: "The Overlook has 110 guest quarters, Derek said.")
    assert r["ok"], r


def test_gibberish_tail_fails():
    text = "So, he kept quiet too."
    r = verify.check_clip(text, clip(text), SR, lambda a, sr: "So, he kept quiet too. He's so lense, have one slat case in.")
    assert not r["ok"] and "words not in the text" in r["reasons"]


def test_skipped_middle_fails():
    text = "The biggest seven banks held on to these rights. Hence, they were called the clearance banks."
    r = verify.check_clip(text, clip(text), SR, lambda a, sr: "The biggest seven banks. Thank you.")
    assert not r["ok"] and "skipped words" in r["reasons"]


def test_abnormal_length_fails():
    text = "He nodded."
    r = verify.check_clip(text, np.zeros(SR * 10, np.float32), SR, lambda a, sr: "He nodded.")
    assert r["reasons"] == ["too long"]


class FakeTTS:
    """Seeds listed in bad_seeds produce a clip that the fake Whisper hears as gibberish."""

    def __init__(self, bad_seeds):
        self.bad_seeds, self.seeds = set(bad_seeds), []

    def cache_key(self, text, seed, voice):
        return hashlib.sha256(json.dumps([text, seed, voice]).encode()).hexdigest()

    def generate(self, text, seed, voice):
        self.seeds.append(seed)
        audio = clip(text)
        if seed in self.bad_seeds:  # twice as long: the fake Whisper hears a babbling tail
            audio = np.concatenate([audio, audio])
        return audio, SR, {"audio_s": len(audio) / SR, "total_s": 0.1, "runaway": False, "attempts": 1}


def fake_check(text, audio, sr):
    bad = len(audio) > 1.5 * len(clip(text))
    return verify.check_clip(text, audio, sr, lambda a, s: text + (" blah blah blah blah" if bad else ""))


def test_speak_regenerates_until_clip_passes(tmp_path):
    text = "It was a quiet night in the old hotel."
    first = render.seed_for("n", 1, 0, 0)
    tts, stats = FakeTTS({first, first + 7919}), render.new_stats()
    render.speak(tts, [("narrator", text)], ("n", 1, 0), 0.3, tmp_path, stats, fake_check)
    assert tts.seeds == [first, first + 7919, first + 2 * 7919]
    assert stats["check_retries"] == 2 and stats["check_failed"] == []


def test_speak_reports_clip_that_never_passes(tmp_path):
    text = "It was a quiet night in the old hotel."
    first = render.seed_for("n", 1, 0, 0)
    tts = FakeTTS({first + 7919 * k for k in range(render.CHECK_ATTEMPTS)})
    stats = render.new_stats()
    render.speak(tts, [("narrator", text)], ("n", 1, 0), 0.3, tmp_path, stats, fake_check)
    assert len(tts.seeds) == render.CHECK_ATTEMPTS
    assert [f["text"] for f in stats["check_failed"]] == [text]


def test_speak_without_check_generates_once(tmp_path):
    tts, stats = FakeTTS(set()), render.new_stats()
    render.speak(tts, [("narrator", "Hello there, friend.")], ("n", 1, 0), 0.3, tmp_path, stats)
    assert len(tts.seeds) == 1 and stats["check_retries"] == 0
