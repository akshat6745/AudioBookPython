import ast
import re
import sys
from pathlib import Path

import calibration
import dataset
import higgs_format as hf
import pipeline_worker
import prepare
import train_worker

MODAL_STAGE_ENTRYPOINTS = ("select", "select_from_rows", "generate", "qa_and_publish",
                           "dataset_publication_plan")
REPO = Path(__file__).resolve().parents[3]
GPU_TTS = REPO / "scripts/gpu_tts"
MODAL_PATHS = (GPU_TTS / "audiobook", GPU_TTS / "student_tts", GPU_TTS)
PIP_NAMES = {"yaml": "pyyaml"}


def _modal_tree():
    return ast.parse((REPO / "modal_student_tts.py").read_text())


def _image_packages(tree, name):
    """pip_install packages of a module-level Modal image, including the image it extends."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            packages, root = set(), node.value
            for call in ast.walk(node.value):
                if isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "pip_install":
                    packages |= {re.split(r"[<>=!~\[ ]", arg.value)[0].lower()
                                 for arg in call.args if isinstance(arg, ast.Constant)}
            while isinstance(root, (ast.Call, ast.Attribute)):
                root = root.func if isinstance(root, ast.Call) else root.value
            if isinstance(root, ast.Name) and root.id != "modal":
                packages |= _image_packages(tree, root.id)
            return packages
    raise AssertionError(f"image {name!r} not found in modal_student_tts.py")


def _third_party_imports(module, seen=None):
    """Third-party modules imported at module level by a local module and its local imports."""
    seen = set() if seen is None else seen
    if module in seen:
        return set()
    seen.add(module)
    path = next((d / f"{module}.py" for d in MODAL_PATHS if (d / f"{module}.py").is_file()), None)
    if path is None:
        return set() if module in sys.stdlib_module_names or module == "__future__" else {module}
    found = set()
    for node in ast.parse(path.read_text()).body:
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level
                 else [])
        for name in names:
            found |= _third_party_imports(name.split(".")[0], seen)
    return found


def test_every_modal_stage_image_installs_its_module_level_imports():
    tree = _modal_tree()
    checked = set()
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        image = next((kw.value.id for d in fn.decorator_list if isinstance(d, ast.Call)
                      for kw in d.keywords if kw.arg == "image"), None)
        if image is None:
            continue
        local = {a.name for node in ast.walk(fn) if isinstance(node, ast.Import) for a in node.names}
        if fn.name in {"train_l40s", "train_l4"}:
            local.add("train_worker")
        packages = _image_packages(tree, image)
        for module in local:
            missing = {PIP_NAMES.get(m, m) for m in _third_party_imports(module)} - packages
            assert not missing, f"{fn.name} ({image}) imports {module} but image lacks {sorted(missing)}"
        checked.add(fn.name)
    assert {"select_dataset", "generate_dataset", "whisper_qa", "train_l40s"} <= checked


def test_generate_dataset_image_installs_pyyaml_for_prepare():
    tree = _modal_tree()
    assert "pyyaml" in _image_packages(tree, "higgs_image")
    assert "yaml" in _third_party_imports("pipeline_worker")


def m(setting, stable, throughput, used, total=48):
    return calibration.Measurement(setting, stable, throughput, used, total, 90)


def test_pipeline_worker_exports_modal_stage_entrypoints():
    for name in MODAL_STAGE_ENTRYPOINTS:
        fn = getattr(pipeline_worker, name, None)
        assert callable(fn), f"pipeline_worker missing Modal stage entrypoint {name!r}"


def test_local_selection_adapter_persists_and_reuses_manifest(tmp_path):
    rows = []
    quotas = {"neutral": 1, "anger": 1, "fear": 1}
    for voice in dataset.VOICES:
        for emotion in quotas:
            rows.append({
                "novel": "fixture", "chapter": 1, "paragraph": len(rows), "chunk": 0,
                "voice": voice, "emotion": emotion, "text": f"{voice} {emotion}",
                "tagged_text": f"<|emotion:{emotion}|>{voice} {emotion}", "estimated_s": 2,
            })
    first = pipeline_worker.select_from_rows(tmp_path, rows, quotas)
    second = pipeline_worker.select_from_rows(tmp_path, [], quotas)
    assert first["manifest"]["clips"] == 6
    assert first["cached"] is False
    assert second == {**first, "cached": True}


def test_qa_resumes_saved_verdicts_and_warns_on_small_dataset(tmp_path, monkeypatch, capsys):
    import json
    import types

    import numpy as np

    rows = [{"id": f"c{i}", "wav": f"raw/c{i}.wav", "text": "hello"} for i in range(3)]
    (tmp_path / "generated.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "qa_results.jsonl").write_text(
        json.dumps({**rows[0], "duration_s": 2.0, "qa": {"ok": True}}) + "\n")
    checked = []
    monkeypatch.setitem(sys.modules, "check_audio", types.SimpleNamespace(
        transcribe_setup=lambda: ("fake", lambda audio, sr: "hello"),
        read_wav=lambda path: (np.zeros(48000, np.float32), 24000)))
    monkeypatch.setitem(sys.modules, "verify", types.SimpleNamespace(
        check_clip=lambda text, audio, sr, transcribe: checked.append(text) or {"ok": True}))
    s3 = types.SimpleNamespace(upload_file=lambda *a, **k: None, put_object=lambda **k: None)
    monkeypatch.setattr(pipeline_worker.cr, "r2_client", lambda: s3)
    monkeypatch.setattr(pipeline_worker.prepare, "prepare_lists", lambda work: {})
    monkeypatch.setattr(pipeline_worker, "dataset_publication_plan", lambda work, prefix, accepted: [])
    monkeypatch.setenv("R2_BUCKET_NAME", "fixture")

    report = pipeline_worker.qa_and_publish(tmp_path, "prefix")

    assert len(checked) == 2  # the clip already in qa_results.jsonl is not re-transcribed
    assert report["accepted"] == 3
    assert "WARNING: QA passed only" in capsys.readouterr().out
    assert len((tmp_path / "qa_results.jsonl").read_text().splitlines()) == 3


def test_cached_manifest_never_fetches_sources(tmp_path, monkeypatch):
    (tmp_path / "request_manifest.jsonl").write_text(
        '{"voice": "narrator", "emotion": "neutral", "estimated_s": 2}\n')

    def fail():
        raise AssertionError("a cached manifest must not re-download R2/D1 sources")

    monkeypatch.setattr(pipeline_worker, "_source_rows", fail)
    monkeypatch.setattr(pipeline_worker.dataset, "summary",
                        lambda rows: {"clips": len(rows), "total_estimated_hours": 12.0, "neutral_audio_pct": 33.0})
    assert pipeline_worker.select(tmp_path)["cached"] is True


def test_training_command_and_environment_are_preflightable(tmp_path):
    config = tmp_path / "config.yml"
    assert train_worker.training_command(config)[1:] == [
        "train_finetune.py", "--config_path", str(config),
    ]
    env = train_worker.training_environment(1, "off", workers=0, prefetch=1)
    assert env["MAX_STEPS"] == "1"
    assert env["TRAIN_WORKERS"] == "0"
    assert env["STYLETTS_AMP"] == "off"


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
