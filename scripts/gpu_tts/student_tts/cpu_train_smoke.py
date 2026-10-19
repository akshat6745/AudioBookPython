#!/usr/bin/env python3
"""Smallest run of the patched StyleTTS2 trainer that walks the train_l40s code paths, on CPU.

  SMOKE_MODE=load  (Docker preflight) build the model exactly as train_finetune.py does: ASR/JDC/PLBERT,
                   the LibriTTS checkpoint and WavLM, optimizers, data loaders; then sample_styletts2.py on
                   the LibriTTS checkpoint. Fits a 5 GB VM in about a minute.
  SMOKE_MODE=full  additionally one tiny epoch (2 steps, diffusion + SLM adversarial on), validation,
                   checkpoint save, a resumed epoch and samples from the trained checkpoint. A CPU training
                   step needs ~13+ GB, so this runs on a host with the memory, not in the small VM.

  STYLETTS_ROOT    pristine StyleTTS2 5cedc71 checkout (default /opt/styletts2, as in the training image)
  STUDENT_TTS_PRETRAINED=<epochs_2nd_00020.pth>  reuse a local LibriTTS checkpoint instead of downloading
Shorter crops and the CPU device are set only here; production config and patch are untouched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parent)]
import prepare  # noqa: E402
from patch_styletts2_cuda import patch  # noqa: E402

STYLE_ROOT = Path(os.environ.get("STYLETTS_ROOT", "/opt/styletts2"))
prepare.BASE_CONFIG = STYLE_ROOT / "Configs" / "config_ft.yml"  # same file train_image reads from /opt/styletts2
MODE = os.environ.get("SMOKE_MODE", "load")
BATCH = 2  # the smallest batch train_l40s may use; validation also needs >= 2 clips per batch
CROP = 100  # frames (12.5 ms); production uses 400. slmadv lengths follow it.
SENTENCES = [
    ("narrator", "The wind blew east across the sand hills, carrying the smell of rain."),
    ("dialogue", "Do you really think the bears have come down from the mountains this year?"),
    ("narrator", "He glanced at his father over the back of the shaggy brown mare."),
    ("dialogue", "Stay close to the cart, and keep your bow ready until we reach the village."),
    ("narrator", "Snarls of last year's bramble spread brown across the stony outcrops."),
    ("dialogue", "If you ask me, the winter has lasted far longer than it has any right to."),
]


def patch_for_cpu(root: Path) -> None:
    """After the production patch: run on CPU, and stop before the first epoch when STYLETTS_LOAD_ONLY is set."""
    train = root / "train_finetune.py"
    text = train.read_text()
    if "# cpu smoke" not in text:
        for old, new in (
            ("    device = 'cuda'\n", "    device = 'cpu'  # cpu smoke\n"),
            ("'gpu': torch.cuda.get_device_name(0)}", "'gpu': 'cpu'}"),
            (".to('cuda')", ".to(device)"),
            ("    for epoch in range(start_epoch, epochs):\n",
             "    if os.environ.get('STYLETTS_LOAD_ONLY'):\n"
             "        print('LOAD_OK', flush=True)\n"
             "        return\n"
             "    for epoch in range(start_epoch, epochs):\n"),
        ):
            assert old in text, old
            text = text.replace(old, new, 1 if "for epoch" in old else -1)
        train.write_text(text)
    for name, old, new in (
        ("Modules/discriminators.py", "self.window.to(y.get_device())", "self.window.to(y.device)"),
        ("Modules/hifigan.py", "torch.ones(1, 1, F0_down).to('cuda')", "torch.ones(1, 1, F0_down).to(F0_curve.device)"),
        ("Modules/hifigan.py", "torch.ones(1, 1, N_down).to('cuda')", "torch.ones(1, 1, N_down).to(N.device)"),
    ):
        path = root / name
        path.write_text(path.read_text().replace(old, new))


def pretrained() -> Path:
    local = os.environ.get("STUDENT_TTS_PRETRAINED")
    if local and Path(local).exists():
        return Path(local)
    from huggingface_hub import hf_hub_download

    # HF_HOME cache: the GitHub preflight mounts a cached directory there so the 771 MB file is fetched once
    return Path(hf_hub_download("yl4579/StyleTTS2-LibriTTS", "Models/LibriTTS/epochs_2nd_00020.pth"))


def write_fixture(data: Path) -> None:
    rng = np.random.default_rng(0)
    rows = []
    for i, (voice, text) in enumerate(SENTENCES):
        seconds = 3.0  # longer than the crop and slmadv's minimum, so both code paths run
        t = np.arange(int(24000 * seconds)) / 24000
        audio = 0.2 * np.sin(2 * np.pi * (140 + 40 * i) * t) + 0.02 * rng.standard_normal(t.size)
        path = data / "raw" / f"clip{i}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as out:
            out.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            out.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        rows.append({"wav": f"raw/clip{i}.wav", "voice": voice, "text": text, "duration_s": seconds})
    (data / "metadata.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    prepare.prepare_lists(data, max_s=16.0)
    # split_lines sends every 50th line to validation; make it 4 train (2 steps) + 2 validation (1 batch)
    lines = (data / "train_list.txt").read_text().splitlines() + (data / "val_list.txt").read_text().splitlines()
    (data / "train_list.txt").write_text("\n".join(lines[:4]) + "\n")
    (data / "val_list.txt").write_text("\n".join(lines[4:]) + "\n")


def write_config(config: Path, data: Path, output: Path, epochs: int, checkpoint: Path) -> None:
    import yaml

    prepare.write_config(config, data, output, batch_size=BATCH, epochs=epochs, pretrained=checkpoint, heavy=True)
    cfg = yaml.safe_load(config.read_text())
    cfg["max_len"] = CROP
    cfg["slmadv_params"].update(min_len=CROP, max_len=CROP + CROP // 4)
    config.write_text(yaml.safe_dump(cfg, sort_keys=False))


def run(cmd: list[str], env: dict, label: str) -> tuple[bool, str]:
    t0 = time.perf_counter()
    done = subprocess.run(cmd, cwd=STYLE_ROOT, env=env, capture_output=True, text=True, timeout=1800)
    log = done.stdout + done.stderr
    print(f"--- {label}: exit {done.returncode} in {time.perf_counter() - t0:.0f}s", flush=True)
    if done.returncode:
        print("\n".join(log.splitlines()[-40:]), flush=True)
    return done.returncode == 0, log


def main() -> int:
    patch(STYLE_ROOT)
    patch_for_cpu(STYLE_ROOT)
    t0, checks = time.perf_counter(), {}
    checkpoint = pretrained()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data, output = tmp / "data", tmp / "out"
        write_fixture(data)
        config = tmp / "config_ft.yml"  # outside log_dir, as train_worker requires
        env = {**os.environ, "MAX_STEPS": "0", "TRAIN_WORKERS": "0", "STYLETTS_AMP": "off", "PYTHONUNBUFFERED": "1",
               "SAMPLE_DEVICE": "cpu"}
        train = [sys.executable, "train_finetune.py", "--config_path", str(config)]
        sample_from = checkpoint

        write_config(config, data, output, 1, checkpoint)
        if MODE == "load":
            ok, log = run(train, {**env, "STYLETTS_LOAD_ONLY": "1"}, "build model + load every checkpoint")
            checks["load"] = ok and "LOAD_OK" in log
        else:
            ok, log = run(train, env, "epoch 1 (2 steps, diffusion + SLM) + validation + save")
            steps = [json.loads(x.removeprefix("SLM_STEPS ")) for x in log.splitlines() if x.startswith("SLM_STEPS ")]
            checks["epoch1"] = ok and "Validation loss" in log and (output / "epoch_2nd_00000.pth").exists()
            checks["slm_ran"] = bool(steps) and steps[-1]["slm_steps"] > 0
            # A retried train_l40s rewrites the config from the newest checkpoint and continues from it.
            write_config(config, data, output, 2, checkpoint)
            ok, log = run(train, env, "epoch 2 resumed from epoch_2nd_00000.pth")
            checks["resume"] = ok and (output / "epoch_2nd_00001.pth").exists()
            sample_from = output / "epoch_2nd_00001.pth"

        samples = tmp / "samples"
        ok, log = run([sys.executable, str(Path(__file__).with_name("sample_styletts2.py")), "--repo", str(STYLE_ROOT),
                       "--config", str(config), "--checkpoint", str(sample_from), "--output", str(samples),
                       "--limit", "1"], env, f"sample_styletts2.py on {sample_from.name}")
        checks["samples"] = ok and len(list(samples.glob("*.wav"))) == 2

    ok = all(checks.values())
    print(f"CPU_TRAIN_SMOKE {MODE} {'ok' if ok else 'FAILED'}: {json.dumps(checks)}, {time.perf_counter() - t0:.0f}s",
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
