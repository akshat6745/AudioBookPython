from pathlib import Path

import calibration
import dataset
import higgs_format as hf
import pipeline_worker
import prepare

MODAL_STAGE_ENTRYPOINTS = ("select", "generate", "qa_and_publish")


def m(setting, stable, throughput, used, total=48):
    return calibration.Measurement(setting, stable, throughput, used, total, 90)


def test_pipeline_worker_exports_modal_stage_entrypoints():
    for name in MODAL_STAGE_ENTRYPOINTS:
        fn = getattr(pipeline_worker, name, None)
        assert callable(fn), f"pipeline_worker missing Modal stage entrypoint {name!r}"


def test_higgs_keeps_64_when_higher_width_does_not_improve_passed_audio():
    chosen = calibration.choose_higgs([m(64, True, 18.0, 39.4), m(72, True, 18.3, 43)])
    assert chosen.setting == 64


def test_higgs_uses_supported_faster_width_but_never_128():
    chosen = calibration.choose_higgs([
        m(64, True, 18.0, 39.4), m(72, True, 19.0, 42), m(80, False, 0, 48), m(128, True, 99, 40),
    ])
    assert chosen.setting == 72


def test_training_selects_fastest_stable_with_headroom():
    chosen = calibration.choose_training([
        m(8, True, 8, 20), m(12, True, 11, 31), m(16, True, 14, 43), m(24, False, 0, 48),
    ])
    assert chosen.setting == 16


def test_training_oom_falls_back_one_supported_step():
    assert calibration.next_smaller_batch(24) == 16
    assert calibration.next_smaller_batch(16) == 12
    assert calibration.next_smaller_batch(12) == 8


def test_balanced_selection_honors_voice_emotion_quotas():
    rows = []
    for voice in dataset.VOICES:
        for emotion in ("neutral", "anger"):
            for idx in range(3):
                rows.append({"voice": voice, "emotion": emotion, "novel": "n", "chapter": 1,
                             "paragraph": idx, "text": f"{voice}-{emotion}-{idx}", "estimated_s": 8})
    chosen = dataset.select_balanced(rows, {"neutral": 2, "anger": 2})
    report = dataset.summary(chosen)
    assert report["clips"] == 8
    assert report["neutral_audio_pct"] == 50
    assert set(report["estimated_hours"]) == {"narrator", "dialogue"}


def test_chunk_estimates_are_bounded_and_restate_emotion():
    units = [f"<|emotion:fear|>{'word ' * 30}.", f"<|emotion:fear|>{'word ' * 30}."]
    chunks = hf.chunks(units, max_chars=180)
    assert all(chunk.startswith("<|emotion:fear|>") for chunk in chunks)
    assert all(dataset.MIN_S <= dataset.estimated_seconds(chunk) <= dataset.MAX_S for chunk in chunks)


def test_styletts2_list_format_uses_two_speaker_ids():
    root = Path("/dataset")
    assert prepare.format_line(root, {"wav": "raw/a.wav", "speaker": "narrator"}, "fəˈnɛtɪks") == \
        "raw/a.wav|fəˈnɛtɪks|0"
    assert prepare.format_line(root, {"wav": "raw/b.wav", "speaker": "dialogue"}, "həˈloʊ") == \
        "raw/b.wav|həˈloʊ|1"
