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

MODAL_STAGE_ENTRYPOINTS = ("select", "select_from_rows", "generate", "qa", "publish", "plan_stages",
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

    report = pipeline_worker.qa(tmp_path)

    assert len(checked) == 2  # the clip already in qa_results.jsonl is not re-transcribed
    assert report["accepted"] == 3
    assert "WARNING: QA passed only" in capsys.readouterr().out
    assert len((tmp_path / "qa_results.jsonl").read_text().splitlines()) == 3


def test_monitor_needs_real_gpu_output_and_stops_on_tracebacks():
    import monitor_student_tts as m

    assert m.observe("STAGE start generation") is None  # printed before the GPU container exists
    assert m.observe('GPU_TELEMETRY {"gpu": "NVIDIA L40S"}') == "gpu"
    assert m.observe("ModuleNotFoundError: No module named 'yaml'") == "failed"


def test_handoff_waits_for_qa_to_return_before_stopping():
    import handoff_student_tts as h

    assert h.observe('GPU_TELEMETRY {"gpu": "NVIDIA L4", "queue_depth": 3480}') is None
    assert h.observe("STAGE start qa") is None
    assert h.observe('STAGE done qa: {"accepted": 5100}') == "qa-finished"
    assert h.observe("[train 23:59:01] GPU NVIDIA L40S 44.4 GB") == "qa-finished"  # attached after QA ended
    assert h.observe("RuntimeError: QA left only 9.10 h; refusing an undersized training set") == "qa-failed"
    assert h.observe("Traceback (most recent call last):") == "qa-failed"  # e.g. KeyError: 'speaker'


def test_qa_builds_missing_lists_from_a_finished_pass_without_transcribing(tmp_path, monkeypatch):
    import json
    import sys as _sys
    import types

    (tmp_path / "metadata.jsonl").write_text(json.dumps(
        {"id": "a", "wav": "raw/a.wav", "voice": "dialogue", "text": "hello", "duration_s": 3.0}) + "\n")

    def no_whisper(*_):
        raise AssertionError("metadata.jsonl already holds the QA verdicts")

    monkeypatch.setitem(_sys.modules, "check_audio", types.SimpleNamespace(transcribe_setup=no_whisper))
    monkeypatch.setitem(_sys.modules, "verify", types.SimpleNamespace(check_clip=no_whisper))
    built = []
    monkeypatch.setattr(pipeline_worker.prepare, "prepare_lists", lambda work: built.append(work) or {"train": 1})
    s3 = types.SimpleNamespace(upload_file=lambda *a, **k: None, put_object=lambda **k: None)
    monkeypatch.setattr(pipeline_worker.cr, "r2_client", lambda: s3)
    monkeypatch.setattr(pipeline_worker, "dataset_publication_plan", lambda work, prefix, accepted: [])
    monkeypatch.setenv("R2_BUCKET_NAME", "fixture")

    report = pipeline_worker.qa(tmp_path)
    assert built and report["accepted"] == 1 and report["lists"] == {"train": 1}


def test_queue_records_the_detached_app_id(tmp_path, monkeypatch):
    import types

    import queue_student_tts as q

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(q, "APP_ID_FILE", tmp_path / "student_tts_app_id.txt")
    output = ["spawned Modal function call fc-1\n",
              "View run at https://modal.com/apps/akshat6745/main/ap-Bbzm6PX7CtwkhvvlWB5zFi\n"]
    fake = types.SimpleNamespace(stdout=iter(output), wait=lambda: 0, returncode=0)
    monkeypatch.setattr(q.subprocess, "Popen", lambda *a, **k: fake)
    request = tmp_path / "request.json"
    request.write_text('{"run": true, "gpu": "l40s"}')
    monkeypatch.setattr(sys, "argv", ["queue", str(request)])
    q.main()
    assert (tmp_path / "student_tts_app_id.txt").read_text() == "ap-Bbzm6PX7CtwkhvvlWB5zFi"


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
    assert calibration.next_smaller_batch(8) == 6
    assert calibration.next_smaller_batch(4) == 2
    try:
        calibration.next_smaller_batch(2)
    except RuntimeError:
        pass
    else:
        raise AssertionError("batch 1 breaks StyleTTS2's diffusion/SLM losses, so 2 is the floor")


def _fake_calibration(monkeypatch, tmp_path, outcomes):
    """Run train_worker._calibrate with _run replaced: outcomes maps (phase, batch) -> (code, metrics, logs)."""
    import pytest
    import yaml

    seen = []

    def fake_run(config, steps, timeout_s, amp, workers=8, prefetch=4):
        cfg = yaml.safe_load(config.read_text())
        seen.append((cfg["probe"], int(cfg["batch_size"])))
        return outcomes[seen[-1]]

    class Monitor:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def summary(self):
            return {"median_utilization_pct": 90}

    monkeypatch.setattr(train_worker, "_run", fake_run)
    monkeypatch.setattr(train_worker.prepare, "write_config",
                        lambda path, data, output, batch, **kw: path.write_text(
                            f"batch_size: {batch}\nprobe: {kw['probe']}\n"))
    monkeypatch.setattr(train_worker.telemetry, "Monitor", Monitor)
    (tmp_path / "out").mkdir(exist_ok=True)

    def calibrate():
        return train_worker._calibrate(tmp_path, tmp_path / "out" / "o", tmp_path / "cal.json", "L40S", 45.0, "off")

    return calibrate, seen, pytest


def test_slm_adversarial_loss_gets_two_clips_at_every_batch_size(tmp_path):
    base = {"data_params": {}, "loss_params": {}, "slmadv_params": {"batch_percentage": 0.5}}
    for batch, expected in ((2, 1.0), (4, 0.5), (6, 0.5), (8, 0.5)):
        config = prepare.build_config(base, tmp_path, tmp_path / "out", batch, 6, tmp_path / "p.pth")
        share = config["slmadv_params"]["batch_percentage"]
        assert share == expected and batch * share >= 2  # slmadv returns None (skips SLM) with <= 1 clip
    assert base["slmadv_params"]["batch_percentage"] == 0.5  # the upstream base config is not mutated


def _ok(examples_per_s, step_s=1.0, vram=10.0):
    return 0, {"examples_per_s": examples_per_s, "step_s": step_s, "peak_vram_gb": vram}, ""


OOM = (1, {}, "torch.OutOfMemoryError: CUDA out of memory.\n")
TIMEOUT = (-15, {}, "TRAIN_PROGRESS\nTIMEOUT\n")


def test_calibration_stops_on_a_crash_that_is_not_out_of_memory(tmp_path, monkeypatch):
    crash = (1, {}, "ModuleNotFoundError: No module named 'pandas'\n")
    calibrate, seen, pytest = _fake_calibration(monkeypatch, tmp_path, {("heavy", 2): crash})
    with pytest.raises(RuntimeError, match="not out of memory[\\s\\S]*pandas"):
        calibrate()
    assert seen == [("heavy", 2)]  # the same import error at every batch size: no further probes


def test_calibration_picks_a_batch_per_phase_with_backoff_and_timeouts(tmp_path, monkeypatch):
    outcomes = {("heavy", 2): _ok(2.0), ("heavy", 4): _ok(3.5), ("heavy", 6): TIMEOUT,
                ("plain", 6): _ok(6.0), ("plain", 8): _ok(7.5), ("plain", 12): OOM}
    calibrate, seen, _ = _fake_calibration(monkeypatch, tmp_path, outcomes)
    choices = calibrate()
    assert choices["heavy"]["setting"] == 4 and choices["plain"]["setting"] == 8
    # plain only probes above the heavy batch: plain's losses are a subset of heavy's
    assert seen == [("heavy", 2), ("heavy", 4), ("heavy", 6), ("plain", 6), ("plain", 8), ("plain", 12)]

    seen.clear()
    assert calibrate() == choices and seen == []  # a retried container reuses both saved choices

    (tmp_path / "cal.json").unlink()
    calibrate, seen, pytest = _fake_calibration(monkeypatch, tmp_path, {("heavy", 2): OOM})
    with pytest.raises(RuntimeError, match="cannot train below batch 2"):
        calibrate()
    assert seen == [("heavy", 2)]


def test_calibration_ignores_the_v1_single_batch_choice(tmp_path, monkeypatch):
    import json

    v1 = {"chosen": {"setting": 2, "stable": True, "throughput": 1.5, "vram_used_gb": 27, "vram_total_gb": 45,
                     "utilization_pct": 60, "step_s": 1.35, "error": ""}, "trace": [], "amp": "off"}
    (tmp_path / "cal.json").write_text(json.dumps({"L40S": {"styletts2": v1}}))
    outcomes = {("heavy", 2): _ok(2.0), ("heavy", 4): OOM, ("plain", 4): _ok(3.0), ("plain", 6): _ok(4.0),
                ("plain", 8): _ok(4.1, vram=43.0)}
    calibrate, seen, _ = _fake_calibration(monkeypatch, tmp_path, outcomes)
    choices = calibrate()
    assert [b for _, b in seen] == [2, 4, 4, 6, 8]  # probed afresh despite the saved v1 batch 2
    assert choices["heavy"]["setting"] == 2 and choices["plain"]["setting"] == 6  # 8 lacks 7.5% headroom
    saved = json.loads((tmp_path / "cal.json").read_text())["L40S"]
    assert saved["styletts2"] == v1 and {"styletts2-v2-heavy", "styletts2-v2-plain"} <= set(saved)


def test_plain_calibration_falls_back_to_the_heavy_batch(tmp_path, monkeypatch):
    calibrate, seen, _ = _fake_calibration(monkeypatch, tmp_path, {
        ("heavy", 2): _ok(2.0, step_s=2.5), ("heavy", 4): OOM, ("plain", 4): TIMEOUT})
    choices = calibrate()
    assert choices["plain"]["setting"] == 2 and choices["plain"]["step_s"] == 2.5
    assert seen[-1] == ("plain", 4)

    (tmp_path / "cal.json").unlink()
    calibrate, seen, _ = _fake_calibration(monkeypatch, tmp_path, {
        ("heavy", 2): _ok(2.0), ("heavy", 4): OOM, ("plain", 4): OOM})
    assert calibrate()["plain"]["setting"] == 2


def test_phase_configs_keep_the_full_schedule(tmp_path):
    base = {"data_params": {}, "loss_params": {}, "slmadv_params": {"batch_percentage": 0.5}}
    assert prepare.phase_epochs(6) == (1, 4)
    first = prepare.build_config(base, tmp_path, tmp_path / "out", 8, 6, tmp_path / "p.pth", stop_epoch=4)
    assert first["epochs"] == 4 and first["batch_size"] == 8
    assert (first["loss_params"]["diff_epoch"], first["loss_params"]["joint_epoch"]) == (1, 4)
    plain = prepare.build_config(base, tmp_path, tmp_path / "out", 8, 1, tmp_path / "p.pth", probe="plain")
    heavy = prepare.build_config(base, tmp_path, tmp_path / "out", 2, 1, tmp_path / "p.pth", probe="heavy")
    assert plain["loss_params"] == {"diff_epoch": 0, "joint_epoch": 1}  # diffusion on, SLM never in epoch 0
    assert heavy["loss_params"] == {"diff_epoch": 0, "joint_epoch": 0}


def test_training_estimate_reports_each_phase(tmp_path):
    (tmp_path / "train_list.txt").write_text("".join(f"raw/{i}.wav|p|0\n" for i in range(101)))
    choices = {"plain": {"setting": 8, "step_s": 1.8}, "heavy": {"setting": 4, "step_s": 3.6}}
    est = train_worker.training_estimate(tmp_path, choices, epochs=6)
    plain, heavy = est["phases"]["plain"], est["phases"]["heavy"]
    assert (plain["epochs"], plain["steps_per_epoch"], heavy["epochs"], heavy["steps_per_epoch"]) == \
        ([0, 4], 12, [4, 6], 25)  # partial last batches are dropped
    assert plain["hours"] == round(4 * 12 * 1.8 / 3600, 2) and heavy["hours"] == round(2 * 25 * 3.6 / 3600, 2)
    assert est["projected_hours"] == round((4 * 12 * 1.8 + 2 * 25 * 3.6) / 3600, 2)
    resumed = train_worker.training_estimate(tmp_path, choices, epochs=6, start_epoch=1)
    assert resumed["phases"]["plain"]["remaining_epochs"] == 3 and resumed["phases"]["heavy"]["remaining_epochs"] == 2


def _fake_training(monkeypatch, tmp_path, oom_at=()):
    """Real prepare.write_config on a minimal base; _run writes one checkpoint per epoch it would train."""
    import yaml

    base = tmp_path / "base.yml"
    base.write_text(yaml.safe_dump({"data_params": {}, "loss_params": {}, "slmadv_params": {"batch_percentage": 0.5}}))
    monkeypatch.setattr(prepare, "BASE_CONFIG", base)
    runs = []

    def fake_run(config, steps, timeout_s, amp, workers=8, prefetch=4):
        cfg = yaml.safe_load(config.read_text())
        out = Path(cfg["log_dir"])
        start = train_worker.next_epoch(out)
        runs.append({"batch": cfg["batch_size"], "start": start, "epochs": cfg["epochs"],
                     "resume": not cfg["load_only_params"], "joint": cfg["loss_params"]["joint_epoch"],
                     "workers": workers})
        if (cfg["batch_size"], start) in oom_at:
            return 1, {}, "torch.OutOfMemoryError: CUDA out of memory.\n"
        for epoch in range(start, cfg["epochs"]):
            (out / f"epoch_2nd_{epoch:05d}.pth").write_bytes(b"")
        return 0, {}, ""

    class Monitor:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def summary(self):
            return {}

    monkeypatch.setattr(train_worker, "_run", fake_run)
    monkeypatch.setattr(train_worker.telemetry, "Monitor", Monitor)
    out = tmp_path / "out"
    out.mkdir()

    def train(**kw):
        choices = {"plain": {"setting": 8, "utilization_pct": 90}, "heavy": {"setting": 4, "utilization_pct": 90}}
        return train_worker.train_phases(tmp_path, out, tmp_path / "config_ft.yml", tmp_path / "p.pth",
                                         choices, "off", epochs=6, **kw)

    return train, runs, out


def test_training_runs_plain_epochs_then_resumes_slm_epochs_at_the_heavy_batch(tmp_path, monkeypatch):
    train, runs, out = _fake_training(monkeypatch, tmp_path)
    report = train()
    assert runs == [
        {"batch": 8, "start": 0, "epochs": 4, "resume": False, "joint": 4, "workers": 8},
        {"batch": 4, "start": 4, "epochs": 6, "resume": True, "joint": 4, "workers": 8},
    ]
    assert {p: r["batch"] for p, r in report.items()} == {"plain": 8, "heavy": 4}
    assert train_worker.next_epoch(out) == 6
    runs.clear()
    assert train() == {} and runs == []  # a retry after both phases trains nothing


def test_training_resumes_mid_phase_and_backs_off_on_oom_per_phase(tmp_path, monkeypatch):
    train, runs, out = _fake_training(monkeypatch, tmp_path, oom_at={(8, 1), (4, 4)})
    (out / "epoch_2nd_00000.pth").write_bytes(b"")  # epoch 0 from an earlier run (e.g. the batch-2 app)
    report = train()
    assert [(r["batch"], r["start"], r["epochs"], r["resume"]) for r in runs] == [
        (8, 1, 4, True), (6, 1, 4, True), (4, 4, 6, True), (2, 4, 6, True)]
    assert runs[1]["workers"] == 8 and {p: r["batch"] for p, r in report.items()} == {"plain": 6, "heavy": 2}


def test_training_fails_fast_on_a_crash_that_is_not_out_of_memory(tmp_path, monkeypatch):
    import pytest

    train, runs, _ = _fake_training(monkeypatch, tmp_path)
    monkeypatch.setattr(train_worker, "_run", lambda *a, **k: runs.append(1) or (1, {}, "KeyError: 'x'\n"))
    with pytest.raises(RuntimeError, match="plain phase exited 1"):
        train()
    assert runs == [1]


def test_pinned_styletts2_lr_is_constant_so_a_batch_change_cannot_reschedule_it():
    import pytest

    upstream = Path("/opt/styletts2")
    if not (upstream / "train_finetune.py").exists():
        pytest.skip("StyleTTS2 checkout only exists in the Docker preflight image")
    train = (upstream / "train_finetune.py").read_text()
    optim = (upstream / "optimizers.py").read_text()
    # Phase 2 resumes AdamW state with a new steps_per_epoch. That is only safe while the OneCycleLR
    # schedulers are flat (div_factor = final_div_factor = 1), never stepped, and not checkpointed.
    assert "div_factor=1," in optim and "final_div_factor=1)" in optim
    assert ".scheduler(" not in train and "scheduler.step" not in train
    assert "state_dicts = [(key, self.optimizers[key].state_dict())" in optim


def test_cuda_patch_applies_to_pinned_styletts2(tmp_path):
    import shutil

    import pytest

    import patch_styletts2_cuda

    upstream = Path("/opt/styletts2")
    if not (upstream / "train_finetune.py").exists():
        pytest.skip("StyleTTS2 checkout only exists in the Docker preflight image")
    for name in ("train_finetune.py", "meldataset.py", "optimizers.py"):
        shutil.copy(upstream / name, tmp_path / name)
    patch_styletts2_cuda.patch(tmp_path)
    patch_styletts2_cuda.patch(tmp_path)  # train_l40s re-runs it on retries
    patched = (tmp_path / "train_finetune.py").read_text()
    compile(patched, "train_finetune.py", "exec")
    assert "TRAIN_METRICS" in patched and "num_workers=train_workers" in patched


def test_styletts2_training_script_imports_in_the_training_image():
    import subprocess

    import pytest

    upstream = Path("/opt/styletts2")
    if not (upstream / "train_finetune.py").exists():
        pytest.skip("StyleTTS2 checkout only exists in the Docker preflight image")
    # Every calibration probe is a fresh `python train_finetune.py`; a missing package (pandas, imported by
    # meldataset.py) makes each one exit in seconds and calibration then finds no stable batch.
    done = subprocess.run([sys.executable, "-c", "import meldataset, models, utils, losses, train_finetune"],
                          cwd=upstream, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]


def test_relaunch_skips_finished_generation_and_qa(tmp_path, monkeypatch):
    import json

    row = {"novel": "n", "chapter": 1, "paragraph": 0, "chunk": 0, "voice": "narrator",
           "tagged_text": "hi", "emotion": "neutral", "estimated_s": 2}
    rid = pipeline_worker._id(row)
    (tmp_path / "request_manifest.jsonl").write_text(json.dumps(row) + "\n")
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / f"{rid}.wav").write_bytes(b"")
    (tmp_path / "generated.jsonl").write_text(json.dumps({**row, "id": rid, "wav": f"raw/{rid}.wav"}) + "\n")

    def no_higgs(cache):
        raise AssertionError("every clip exists; the Higgs server must not start")

    monkeypatch.setattr(pipeline_worker.cr, "start_higgs", no_higgs)
    assert pipeline_worker.generate(tmp_path, tmp_path / "cache")["cached"] is True

    (tmp_path / "metadata.jsonl").write_text(json.dumps({"id": rid, "duration_s": 7200.0}) + "\n")
    for name in pipeline_worker.LISTS:
        (tmp_path / name).write_text("x\n")
    qa = pipeline_worker.qa(tmp_path)
    assert qa == {"accepted": 1, "passed_hours": 2.0, "cached": True}
    assert pipeline_worker.stage_status(tmp_path) == {"generated": True, "qa_done": True, "lists": True}


def test_orchestrator_only_pays_for_unfinished_stages():
    plan = pipeline_worker.plan_stages
    fresh = {"generated": False, "qa_done": False, "lists": False}
    assert plan(fresh) == {"generation": "gpu", "qa": "gpu"}
    assert plan({**fresh, "generated": True}) == {"generation": "skip", "qa": "gpu"}
    # QA finished but crashed building lists (the KeyError run): lists on CPU, no Higgs, no Whisper
    assert plan({"generated": True, "qa_done": True, "lists": False}) == {"generation": "skip", "qa": "cpu"}
    assert plan({"generated": True, "qa_done": True, "lists": True}) == {"generation": "skip", "qa": "skip"}
    # metadata.jsonl means the WAVs were checked, so generation is never re-run for a finished QA
    assert plan({"generated": False, "qa_done": True, "lists": True})["generation"] == "skip"


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
    # Rows carry "voice" (from dataset.candidates), the same key production metadata.jsonl has.
    assert prepare.format_line(root, {"wav": "raw/a.wav", "voice": "narrator"}, "fəˈnɛtɪks") == \
        "raw/a.wav|fəˈnɛtɪks|0"
    assert prepare.format_line(root, {"wav": "raw/b.wav", "voice": "dialogue"}, "həˈloʊ") == \
        "raw/b.wav|həˈloʊ|1"
