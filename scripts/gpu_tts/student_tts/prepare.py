"""Finalize accepted WAVs into StyleTTS2 metadata/list/config files."""
from __future__ import annotations

import json
import random
from pathlib import Path

import yaml

SPEAKERS = {"narrator": 0, "dialogue": 1}
BASE_CONFIG = Path("/opt/styletts2/Configs/config_ft.yml")


def format_line(data: Path, row: dict, phonemes: str) -> str:
    return f"{row['wav']}|{phonemes.strip()}|{SPEAKERS[row['speaker']]}"


def split_lines(lines: list[str], every: int = 50) -> tuple[list[str], list[str]]:
    """Deterministic shuffle, then every 50th line to validation (2%, at least one)."""
    lines = sorted(lines)
    random.Random(0).shuffle(lines)
    val = lines[::every]
    train = [line for i, line in enumerate(lines) if i % every]
    return train, val


def prepare_lists(data: Path, min_s: float = 1.0, max_s: float = 16.0) -> dict:
    from phonemizer.backend import EspeakBackend

    rows = [json.loads(line) for line in (data / "metadata.jsonl").read_text().splitlines() if line.strip()]
    rows = [r for r in rows if min_s <= r["duration_s"] <= max_s and r["text"]]
    backend = EspeakBackend("en-us", preserve_punctuation=True, with_stress=True)
    phones = backend.phonemize([r["text"] for r in rows], njobs=8)
    lines = [format_line(data, r, p) for r, p in zip(rows, phones) if p.strip()]
    train, val = split_lines(lines)
    (data / "train_list.txt").write_text("\n".join(train) + "\n")
    (data / "val_list.txt").write_text("\n".join(val) + "\n")
    ood = [p.strip() for p in phones if 50 <= len(p.strip()) <= 300]
    (data / "OOD_texts.txt").write_text("\n".join(f"{p}|0" for p in ood) + "\n")
    return {"train": len(train), "val": len(val), "ood": len(ood),
            "hours": round(sum(r["duration_s"] for r in rows) / 3600, 2)}


def build_config(base: dict, data: Path, output: Path, batch_size: int, epochs: int, pretrained: Path,
                 heavy: bool = False) -> dict:
    """Upstream ft config with our data. Resumes from the newest epoch_2nd_*.pth in output when present.

    Upstream (1 h of data) uses 50 epochs with diffusion from 10 and joint SLM training from 30;
    the same 20%/60% proportions over 15 epochs give 3 and 9. heavy=True starts both at epoch 0
    so a short calibration probe measures the most memory-hungry training step.
    """
    config = json.loads(json.dumps(base))
    checkpoints = sorted(output.glob("epoch_2nd_*.pth"))
    config.update({
        "log_dir": str(output), "save_freq": 1, "epochs": epochs, "batch_size": batch_size, "max_len": 400,
        "pretrained_model": str(checkpoints[-1] if checkpoints else pretrained),
        "second_stage_load_pretrained": True,
        "load_only_params": not checkpoints,
        "data_params": {**config["data_params"], "train_data": str(data / "train_list.txt"),
                        "val_data": str(data / "val_list.txt"), "OOD_data": str(data / "OOD_texts.txt"),
                        "root_path": str(data)},
    })
    config["loss_params"]["diff_epoch"] = 0 if heavy else round(epochs * 0.2)
    config["loss_params"]["joint_epoch"] = 0 if heavy else round(epochs * 0.6)
    return config


def write_config(path: Path, data: Path, output: Path, batch_size: int, epochs: int = 15,
                 pretrained: Path = Path("/workspace/cache/styletts2/Models/LibriTTS/epochs_2nd_00020.pth"),
                 heavy: bool = False) -> None:
    """path must not be inside output: train_finetune.py copies the config into log_dir under its basename."""
    if path.resolve().parent == output.resolve():
        raise ValueError("config path must be outside log_dir")
    output.mkdir(parents=True, exist_ok=True)
    config = build_config(yaml.safe_load(BASE_CONFIG.read_text()), data, output, batch_size, epochs,
                          pretrained, heavy)
    path.write_text(yaml.safe_dump(config, sort_keys=False))
