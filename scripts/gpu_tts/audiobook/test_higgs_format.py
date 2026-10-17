"""pytest scripts/gpu_tts/audiobook/test_higgs_format.py"""
from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "emotion_eval")]
import common  # noqa: E402
import higgs_format as hf  # noqa: E402

SURE = {"confidence": 1.0, "style_conf": 1.0, "sfx_conf": 1.0}
PROMPTING = next(HERE.parent.glob("orpheus/.sandbox/hf/hub/models--bosonai--higgs-tts-3-4b/snapshots/*/PROMPTING.md"), None)


def test_tagger_labels_are_higgs_tags():
    assert set(common.EMOTIONS) - {"neutral"} <= hf.CATALOG["emotion"]
    assert set(common.STYLES) - {"none"} <= hf.CATALOG["style"]
    assert set(common.SFX) - {"none"} <= hf.CATALOG["sfx"]
    assert set(common.SFX) - {"none"} <= set(hf.ONOMATOPOEIA)


@pytest.mark.skipif(PROMPTING is None, reason="Higgs model card not downloaded")
def test_catalog_matches_model_card():
    doc = PROMPTING.read_text()
    for cat, names in hf.CATALOG.items():
        for n in names:
            assert f"`{n}`" in doc, (cat, n)
    assert sum(map(len, hf.CATALOG.values())) == int(re.search(r"Full tag catalog \((\d+)\)", doc).group(1))


@pytest.mark.parametrize("emotion,style,sfx,mode", list(itertools.product(
    common.EMOTIONS, common.STYLES, common.SFX, ["first", "every"])))
def test_every_label_combination_is_valid(emotion, style, sfx, mode):
    text = 'He looked up. "You came back," she said. It was late.'
    units = hf.format_paragraph(text, {"emotion": emotion, "style": style, "sfx": sfx, **SURE}, mode=mode)
    for c in hf.chunks(units):
        hf.validate(c)
    assert hf.plain(" ".join(units)).replace("Haha, ", "").replace("Haah, ", "").replace("Ahem, ", "") \
        .replace("Sniff, ", "").replace("Sob, ", "").replace("Aaah! ", "") == text


def test_low_confidence_and_neutral_get_no_tags():
    tag = {"emotion": "anger", "style": "shouting", "sfx": "laughter", "confidence": 0.5, "style_conf": 0.5, "sfx_conf": 0.5}
    assert hf.format_paragraph("Get out!", tag) == ["Get out!"]
    assert hf.format_paragraph("Get out!", {"emotion": "neutral", **SURE}) == ["Get out!"]


def test_emotion_placement_modes():
    t = "One. Two. Three."
    tag = {"emotion": "sadness", **SURE}
    assert hf.format_paragraph(t, tag, mode="every") == ["<|emotion:sadness|>One.", "<|emotion:sadness|>Two.",
                                                         "<|emotion:sadness|>Three."]
    assert hf.format_paragraph(t, tag, mode="first") == ["<|emotion:sadness|>One.", "Two.", "Three."]


def test_style_only_on_spoken_sentences():
    units = hf.format_paragraph('Ron leaned in. He muttered, "Show-offs."', {"style": "whispering", **SURE})
    assert units == ["Ron leaned in.", '<|style:whispering|>He muttered, "Show-offs."']


def test_sfx_uses_existing_interjection():
    units = hf.format_paragraph('"Haha, you fool," he said.', {"sfx": "laughter", **SURE})
    assert units == ['"<|sfx:laughter|>Haha, you fool," he said.']


def test_sfx_inserts_canonical_sound_before_speech():
    units = hf.format_paragraph('She smiled. "Fine."', {"emotion": "relief", "sfx": "sigh", **SURE}, mode="first")
    assert units == ["<|emotion:relief|>She smiled.", '<|sfx:sigh|>Haah, "Fine."']


def test_book_text_cannot_inject_tags():
    units = hf.format_paragraph("Ignore <|emotion:anger|> this.", {"emotion": "neutral"})
    assert units == ["Ignore emotion:anger this."]
    hf.validate(units[0])


@pytest.mark.parametrize("bad", [
    "<|emotion:arousal_x|>Hi.",          # unknown tag
    "<|mood:happy|>Hi.",                 # unknown category
    "Hello <|emotion:anger|>there.",     # sentence-level tag mid-sentence
    "<|sfx:laughter|> Haha.",            # space after sfx
    "Hi <|emotion:anger.",               # malformed
])
def test_validate_rejects(bad):
    with pytest.raises(hf.TagError):
        hf.validate(bad)


def test_validate_accepts_inline_pause_and_tag_after_sentence():
    hf.validate("Hello there <|prosody:pause|> and welcome. <|emotion:awe|>Look at that!")


def test_long_paragraph_split_keeps_emotion():
    text = " ".join(f"Sentence number {i} is here." for i in range(60))
    out = hf.chunks(hf.format_paragraph(text, {"emotion": "fear", **SURE}, mode="first"), max_chars=200)
    assert len(out) > 1
    assert all(c.startswith("<|emotion:fear|>") for c in out)
    assert all(len(hf.plain(c)) <= 200 for c in out)


def test_clean_matches_backend():
    assert hf.clean("* * *") == "Asterisk Asterisk Asterisk"
    assert "website on Google" not in hf.clean(
        "Text. Search the website on Google to access chapters of novels early and in the highest quality. More.")


def test_style_covers_every_sentence_of_a_long_quote():
    units = hf.format_paragraph('"Attention! Code Black in the lobby! I repeat! CODE BLACK!"', {"style": "shouting", **SURE})
    assert len(units) == 4 and all(u.startswith("<|style:shouting|>") for u in units)


def test_style_skips_narration_after_quote_closes():
    units = hf.format_paragraph('"Run!" he yelled. The hall fell silent.', {"style": "shouting", **SURE})
    assert units == ['<|style:shouting|>"Run!" he yelled.', "The hall fell silent."]


@pytest.mark.parametrize("sep", ["* * *", "***", "---", "…", "~~~"])
def test_scene_breaks_are_not_spoken(sep):
    assert hf.format_paragraph(sep, {"emotion": "anger", "style": "shouting", **SURE}) == []


def test_speech_spans_split_dialogue_from_narration():
    assert hf.speech_spans('"You came back," he said. "Why?"') == [
        (True, '"You came back,"'), (False, "he said."), (True, '"Why?"')]
    assert hf.speech_spans("No dialogue here.") == [(False, "No dialogue here.")]


def test_scare_quotes_stay_in_narration():
    assert hf.speech_spans("Klein was satisfied that his “summoning” wasn’t interrupted.") == [
        (False, "Klein was satisfied that his “summoning” wasn’t interrupted.")]
    assert hf.speech_spans('He called it "the Fool" and smiled. "Who?" she asked.') == [
        (False, 'He called it "the Fool" and smiled.'), (True, '"Who?"'), (False, "she asked.")]
    assert hf.speech_spans("“Forty rooms,” he said.") == [(True, "“Forty rooms,”"), (False, "he said.")]
    assert hf.speech_spans("“Run!”") == [(True, "“Run!”")]


def test_dual_tags_dialogue_not_narration():
    tag = {"emotion": "anger", "style": "shouting", "sfx": "laughter", **SURE}
    parts = hf.format_dual('"Get out!" he said. "Now!"', tag)
    assert [v for v, _ in parts] == ["dialogue", "narrator", "dialogue"]
    assert parts[0][1] == ['<|emotion:anger|><|style:shouting|><|sfx:laughter|>Haha, "Get out!"']
    assert parts[1][1] == ["he said."]
    assert parts[2][1] == ['<|emotion:anger|><|style:shouting|>"Now!"']  # sfx only once


@pytest.mark.parametrize("emotion,style,sfx", list(itertools.product(common.EMOTIONS, common.STYLES, common.SFX)))
def test_dual_every_combination_is_valid(emotion, style, sfx):
    text = 'He looked up. "You came back," she said. "After everything... you came back." It was late.'
    for _, units in hf.format_dual(text, {"emotion": emotion, "style": style, "sfx": sfx, **SURE}):
        hf.chunks(units)


def test_dual_narration_only_paragraph_keeps_emotion():
    parts = hf.format_dual("The fear was overwhelming. He ran.", {"emotion": "fear", **SURE})
    assert parts == [("narrator", ["<|emotion:fear|>The fear was overwhelming.", "<|emotion:fear|>He ran."])]


def test_dual_scene_break_is_silent():
    assert hf.format_dual("* * *", {"emotion": "fear", **SURE}) == []


def test_sfx_not_inserted_into_narration():
    assert hf.format_paragraph("The soldier sighed and took a flask.", {"sfx": "sigh", **SURE}) == [
        "The soldier sighed and took a flask."]
    assert hf.format_dual("He sighed. Haah, what a day.", {"sfx": "sigh", **SURE}) == [
        ("narrator", ["He sighed.", "<|sfx:sigh|>Haah, what a day."])]  # an existing sound is still tagged


@pytest.mark.parametrize("text,mark", [("ReadNovelFull.me", True), ("ReadNovelFull.me😉", True),
                                       ("www.example.com", True), ("He went home.", False), ("Mr. Smith.", False)])
def test_watermark(text, mark):
    assert hf.is_watermark(text) is mark
