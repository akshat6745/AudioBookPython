#!/usr/bin/env python3
"""CPU-only end-to-end preflight for the Modal student-TTS pipeline."""
from __future__ import annotations

import ast
import importlib
import json
import math
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import torch
import yaml

import calibration
import cloud_render
import dataset
import pipeline_worker
import prepare
import train_worker

REPO = Path(os.environ.get("STUDENT_TTS_REPO", "/workspace/repo")).resolve()
WORK = Path(os.environ.get("STUDENT_TTS_FIXTURE_DIR", "/tmp/student-tts-smoke")).resolve()
PREFIX = "student_tts/styletts2-eye-v1-smoke"
QUOTAS = {"neutral": 2, "anger": 2, "fear": 2}

MODULE_CONTRACTS = {
    "pipeline_worker": ("select", "select_from_rows", "generate", "qa", "publish", "plan_stages", "dataset_publication_plan"),
    "prepare": ("prepare_lists", "write_config", "build_config"),
    "train_worker": ("train", "training_command", "training_environment"),
    "calibration": ("choose_higgs", "choose_training", "next_smaller_batch"),
    "sample_styletts2": ("main",),
    "download_and_infer": ("main",),
    "cloud_render": ("r2_client", "d1_query", "start_higgs"),
    "check_audio": ("transcribe_setup", "read_wav"),
    "verify": ("check_clip",),
    "synth": ("SglangHiggs",),
}


def check_contracts() -> None:
    for module_name, attributes in MODULE_CONTRACTS.items():
        module = importlib.import_module(module_name)
        for attribute in attributes:
            value = getattr(module, attribute, None)
            assert callable(value), f"{module_name}.{attribute} is missing or not callable"

    modal_module = importlib.import_module("modal_student_tts")
    for name in ("select_dataset", "generate_dataset", "whisper_qa", "finish_qa", "publish_dataset", "train_l40s", "train_l4", "orchestrate"):
        assert hasattr(modal_module, name), f"modal_student_tts.{name} is missing"

    tree = ast.parse((REPO / "modal_student_tts.py").read_text())
    functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"pipeline", "orchestrate", "select_dataset", "generate_dataset", "whisper_qa"} <= functions

    required = (
        "scripts/gpu_tts/student_tts/pipeline_worker.py",
        "scripts/gpu_tts/student_tts/train_worker.py",
        "scripts/gpu_tts/student_tts/sample_styletts2.py",
        "scripts/gpu_tts/audiobook/cloud_render.py",
        "scripts/gpu_tts/audiobook/voices/default_seed8.wav",
        "scripts/gpu_tts/audiobook/voices/default_seed2.wav",
        "scripts/gpu_tts/texts/passage.txt",
        "scripts/gpu_tts/higgs_v3/setup.sh",
        "scripts/gpu_tts/higgs_v3/serve.sh",
        "student_tts_request.json",
    )
    missing = [path for path in required if not (REPO / path).is_file()]
    assert not missing, f"files omitted from Modal/Docker context: {missing}"

    import monotonic_align  # noqa: F401
    style_root = Path("/opt/styletts2")
    sys.path.insert(0, str(style_root))
    from models import build_model  # noqa: F401
    assert (style_root / "train_finetune.py").is_file()
    assert (style_root / "Configs/config_ft.yml").is_file()


def fixture_rows() -> list[dict]:
    rows = []
    text = (
        "The measured voice remains clear and expressive while this tiny deterministic "
        "fixture checks the complete local pipeline contract."
    )
    for voice in dataset.VOICES:
        for emotion in QUOTAS:
            for index in range(2):
                tagged = f"<|emotion:{emotion}|>{text} Example {index}."
                rows.append({
                    "novel": "local-fixture", "chapter": 1, "paragraph": len(rows), "chunk": 0,
                    "voice": voice, "emotion": emotion, "text": f"{text} Example {index}.",
                    "tagged_text": tagged, "estimated_s": 1.1,
                })
    return rows


class FakeHiggsBoundary:
    """Deterministic replacement for only the unavailable CUDA/SGLang boundary."""

    def generate(self, text: str, seed: int, voice: str) -> tuple[np.ndarray, int, dict]:
        sample_rate = 24000
        frames = int(sample_rate * 1.1)
        frequency = (180 if voice == "narrator" else 240) + seed % 20
        t = np.arange(frames, dtype=np.float32) / sample_rate
        audio = (0.05 * np.sin(2 * math.pi * frequency * t)).astype(np.float32)
        return audio, sample_rate, {"adapter": "deterministic-cpu-fake", "text_chars": len(text)}


def fake_qa_boundary(generated: list[dict]) -> list[dict]:
    """Accept valid local WAVs; Whisper inference is the second unavailable GPU boundary."""
    accepted = []
    for row in generated:
        path = WORK / row["wav"]
        with __import__("wave").open(str(path), "rb") as wav:
            assert wav.getframerate() == 24000
            assert wav.getnchannels() == 1
            duration = wav.getnframes() / wav.getframerate()
        accepted.append({**row, "duration_s": duration,
                         "qa": {"ok": True, "adapter": "deterministic-cpu-fake"}})
    return accepted


def validate_secrets_and_publication(accepted: list[dict]) -> dict:
    expected = set(cloud_render.SECRETS)
    assert expected == {
        "CF_ACCOUNT_ID", "D1_DATABASE_ID", "CF_API_TOKEN", "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME",
    }
    present = sorted(name for name in expected if os.environ.get(name))
    plan = pipeline_worker.dataset_publication_plan(WORK, PREFIX, accepted)
    for local, key, content_type in plan:
        assert local.is_file(), local
        assert key.startswith(PREFIX + "/dataset/")
        assert ".." not in key and not key.startswith("/")
        assert content_type in {"audio/wav", "application/json", "text/plain"}
    return {"expected_secret_names": len(expected), "present_secret_names": len(present),
            "planned_uploads": len(plan)}


def main() -> None:
    assert os.environ.get("STUDENT_TTS_SOURCE_ADAPTER") == "local-fixture"
    assert os.environ.get("STUDENT_TTS_PUBLISH_ADAPTER") == "dry-run"
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)

    check_contracts()
    selection = pipeline_worker.select_from_rows(WORK, fixture_rows(), QUOTAS)
    assert selection["manifest"]["clips"] == 12
    assert set(selection["manifest"]["estimated_hours"]) == set(dataset.VOICES)
    selected = [
        json.loads(line) for line in (WORK / "request_manifest.jsonl").read_text().splitlines() if line.strip()
    ]

    generated, generation_metrics = pipeline_worker._run_rows(
        FakeHiggsBoundary(), selected, WORK / "raw", workers=2,
    )
    (WORK / "generated.jsonl").write_text("".join(json.dumps(row) + "\n" for row in generated))
    accepted = fake_qa_boundary(generated)
    (WORK / "metadata.jsonl").write_text("".join(json.dumps(row) + "\n" for row in accepted))

    lists = prepare.prepare_lists(WORK)
    assert lists == {"train": 11, "val": 1, "ood": 12, "hours": 0.0}
    config = WORK / "config_ft.yml"
    output = WORK / "training-output"
    checkpoint = WORK / "pretrained-smoke.pth"
    torch.save({"net": {}}, checkpoint)
    assert "net" in torch.load(checkpoint, map_location="cpu", weights_only=True)
    prepare.write_config(config, WORK, output, batch_size=2, epochs=1, pretrained=checkpoint)
    loaded = yaml.safe_load(config.read_text())
    assert loaded["batch_size"] == 2
    assert loaded["data_params"]["root_path"] == str(WORK)
    command = train_worker.training_command(config)
    environment = train_worker.training_environment(1, "off", workers=0, prefetch=1)
    assert command[1:] == ["train_finetune.py", "--config_path", str(config)]
    assert environment["MAX_STEPS"] == "1" and environment["STYLETTS_AMP"] == "off"

    publication = validate_secrets_and_publication(accepted)
    report = {
        "status": "ok", "clips": len(accepted), "voices": sorted({row["voice"] for row in accepted}),
        "emotions": sorted({row["emotion"] for row in accepted}), "lists": lists,
        "generation_boundary": "deterministic CPU fake", "qa_boundary": "deterministic CPU fake",
        "generation_metrics": generation_metrics, "publication": publication,
        "styletts2": "imports/config/command/checkpoint-schema validated; CUDA training not executed",
    }
    print("LOCAL_SMOKE " + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
