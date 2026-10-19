"""CUDA calibration, resumable StyleTTS2 training, and R2 publication."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import calibration
import cloud_render as cr
import prepare
import telemetry

STYLE_ROOT = Path("/opt/styletts2")
CANDIDATES = calibration.TRAINING_BATCHES  # probe upward from 2, the smallest batch StyleTTS2 can train
CALIBRATION_BUDGET_S = 15 * 60
CALIBRATION_STEPS = 25
# ~11 h of audio x 6 epochs ~= 66 audio-hours seen, more than upstream's 1 h x 50 epochs. 15 epochs projected
# to 15-30 L40S hours, past the 23 h function cap.
EPOCHS = int(os.environ.get("STUDENT_TTS_EPOCHS", "6"))
JOINT_EPOCH_COST = 1.5  # assumed slowdown of SLM adversarial epochs; only used for the logged estimate


def log(msg: str) -> None:
    print(f"[train {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def training_command(config: Path) -> list[str]:
    """Return the exact StyleTTS2 command used by remote training."""
    return [sys.executable, "train_finetune.py", "--config_path", str(config)]


def training_environment(max_steps: int, amp: str, workers: int = 8, prefetch: int = 4) -> dict[str, str]:
    """Return non-secret training overrides, separately testable in CPU preflight."""
    return {
        "MAX_STEPS": str(max_steps), "TRAIN_WORKERS": str(workers),
        "TRAIN_PREFETCH": str(prefetch), "STYLETTS_AMP": amp,
        "PYTHONUNBUFFERED": "1", "TOKENIZERS_PARALLELISM": "false",
    }


def training_estimate(data: Path, batch: int, step_s: float, epochs: int) -> dict:
    """Projected wall time from the calibration step time, logged before the paid full run starts."""
    clips = sum(1 for line in (data / "train_list.txt").read_text().splitlines() if line.strip())
    steps = -(-clips // batch)
    joint = epochs - round(epochs * 0.6)
    epoch_h = steps * step_s / 3600
    hours = epoch_h * (epochs - joint) + epoch_h * JOINT_EPOCH_COST * joint
    return {"clips": clips, "batch": batch, "steps_per_epoch": steps, "step_s": step_s, "epochs": epochs,
            "projected_hours": round(hours, 2)}


def _gpu_identity() -> tuple[str, float]:
    sample = telemetry.gpu_sample() or {}
    return sample.get("gpu", "unknown"), sample.get("vram_total_mb", 0) / 1024


def _run(config: Path, max_steps: int, timeout_s: int, amp: str, workers: int = 8,
         prefetch: int = 4) -> tuple[int, dict, str]:
    env = {**os.environ, **training_environment(max_steps, amp, workers, prefetch)}
    proc = subprocess.Popen(
        training_command(config),
        cwd=STYLE_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    metrics, tail, deadline = {}, [], time.monotonic() + timeout_s
    assert proc.stdout
    try:
        for line in proc.stdout:
            print(line, end="", flush=True)
            tail = (tail + [line])[-200:]
            if line.startswith("TRAIN_METRICS "):
                metrics = json.loads(line.removeprefix("TRAIN_METRICS "))
            if time.monotonic() > deadline:
                proc.terminate()
                tail.append("TIMEOUT\n")
                break
        code = proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    return code, metrics, "".join(tail)


def _is_oom(logs: str) -> bool:
    return "out of memory" in logs.lower() or "outofmemoryerror" in logs.lower()


def _raise_unless_oom(code: int, stable: bool, oom: bool, logs: str, batch: int) -> None:
    """Only OOM means "try a smaller batch". Any other probe failure (missing package, unloadable checkpoint)
    fails the same way at every batch size, so stop and show its error instead of probing on."""
    if not stable and not oom:
        tail = "\n".join(logs.splitlines()[-30:])
        raise RuntimeError(f"calibration probe at batch {batch} failed (exit {code}), not out of memory:\n{tail}")


def _calibrate(data: Path, output: Path, state: Path, gpu_name: str, total_vram: float,
               default_amp: str) -> dict:
    saved = calibration.load_choice(state, gpu_name, "styletts2")
    if saved:
        log(f"reusing saved calibration for {gpu_name}: {json.dumps(saved['chosen'])}")
        return saved["chosen"] | {"amp": saved.get("amp", "off")}
    deadline = time.monotonic() + CALIBRATION_BUDGET_S
    trace: list[calibration.Measurement] = []
    config = output.parent / "calibration.yml"
    amp = default_amp
    for batch in CANDIDATES:
        remaining = int(deadline - time.monotonic())
        if remaining < 120:
            log(f"calibration budget spent before batch {batch}")
            break
        # Diffusion and SLM adversarial losses run from step 0 so the probe sees the heaviest epoch's memory.
        prepare.write_config(config, data, output / "calibration-output", batch, epochs=1, heavy=True)
        with telemetry.Monitor(lambda: 1, interval_s=2) as monitor:
            code, metrics, logs = _run(config, CALIBRATION_STEPS, min(420, remaining), amp)
        if code != 0 and not _is_oom(logs) and amp != "off" and not trace:
            log(f"batch {batch} failed under {amp} autocast (not OOM); falling back to fp32+TF32")
            amp = "off"
            with telemetry.Monitor(lambda: 1, interval_s=2) as monitor:
                code, metrics, logs = _run(config, CALIBRATION_STEPS, min(420, int(deadline - time.monotonic())), amp)
        oom = _is_oom(logs)
        stable = code == 0 and bool(metrics) and not oom
        m = calibration.Measurement(
            setting=batch, stable=stable, throughput=round(metrics.get("examples_per_s", 0), 3),
            vram_used_gb=round(metrics.get("peak_vram_gb", total_vram), 2), vram_total_gb=round(total_vram, 2),
            utilization_pct=monitor.summary().get("median_utilization_pct", 0),
            step_s=round(metrics.get("step_s", 0), 3), error="oom" if oom else ("" if stable else f"exit-{code}"),
        )
        trace.append(m)
        log("CALIBRATION " + json.dumps({**m.__dict__, "amp": amp, "headroom_pct": round(m.headroom_pct, 1),
                                         "slm_steps": metrics.get("slm_steps")}))
        if logs.rstrip().endswith("TIMEOUT") and any(t.stable for t in trace):
            log(f"batch {batch} probe ran past its time limit; keeping the smaller batches")
            break
        _raise_unless_oom(code, stable, oom, logs, batch)
        if not stable or m.headroom_pct < 7.5:
            break  # larger batches only use more memory
    if not any(m.stable for m in trace):
        raise RuntimeError(f"batch {CANDIDATES[0]} ran out of memory at max_len 400 with the SLM losses on; "
                           "StyleTTS2 cannot train below batch 2, so lower max_len instead")
    chosen = calibration.choose_training(trace)
    calibration.save_choice(state, gpu_name, "styletts2", chosen, trace, extra={"amp": amp})
    log(f"CALIBRATION_CHOICE batch={chosen.setting} amp={amp} examples/s={chosen.throughput} "
        f"vram={chosen.vram_used_gb}/{chosen.vram_total_gb} GB util={chosen.utilization_pct}%")
    return chosen.__dict__ | {"amp": amp}


def _download_pretrained(cache: Path) -> Path:
    from huggingface_hub import hf_hub_download

    target = cache / "styletts2"
    path = target / "Models/LibriTTS/epochs_2nd_00020.pth"
    if not path.exists():
        hf_hub_download("yl4579/StyleTTS2-LibriTTS", "Models/LibriTTS/epochs_2nd_00020.pth", local_dir=target)
    return path


def _upload(s3, bucket: str, files: list[tuple[Path, str, str]]) -> None:
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda f: s3.upload_file(str(f[0]), bucket, f[1], ExtraArgs={"ContentType": f[2]}), files))


def train(work: Path, cache: Path, prefix: str, gpu_kind: str) -> dict:
    work, cache = work.resolve(), cache.resolve()
    output = cache / "styletts2-eye-v1" / f"training-{gpu_kind.lower()}"
    output.mkdir(parents=True, exist_ok=True)
    pretrained = _download_pretrained(cache)
    from patch_styletts2_cuda import patch

    patch(STYLE_ROOT)
    gpu_name, total_vram = _gpu_identity()
    log(f"GPU {gpu_name} {total_vram:.1f} GB; data {work}")
    # StyleTTS2's STFT losses reject bf16/fp16 tensors and its README warns of NaN losses under mixed
    # precision, so train in fp32 with TF32 matmuls instead of spending a probe discovering that.
    chosen = _calibrate(work, output, cache / "calibration.json", gpu_name, total_vram, "off")
    batch, amp = int(chosen["setting"]), chosen["amp"]
    log("TRAIN_ESTIMATE " + json.dumps(training_estimate(work, batch, chosen.get("step_s", 0), EPOCHS)))
    underfed = chosen.get("utilization_pct", 100) < 85
    workers, prefetch = (12, 6) if underfed else (8, 4)
    config = output.parent / "config_ft.yml"  # train_finetune.py writes the trained copy to output/config_ft.yml
    while True:
        prepare.write_config(config, work, output, batch, epochs=EPOCHS, pretrained=pretrained)
        log(f"TRAIN_START batch={batch} amp={amp} workers={workers} prefetch={prefetch} epochs={EPOCHS}")
        with telemetry.Monitor(lambda: 1, interval_s=30) as monitor:
            code, _, logs = _run(config, 0, 23 * 3600, amp, workers, prefetch)
        if code == 0:
            break
        if not _is_oom(logs):
            raise RuntimeError(f"StyleTTS2 exited {code}")
        batch = calibration.next_smaller_batch(batch)
        workers, prefetch = 8, 2
        log(f"OOM during training; resuming from the last checkpoint with batch {batch}")

    checkpoints = sorted(output.glob("epoch_2nd_*.pth"))
    if not checkpoints:
        raise RuntimeError("training completed without a checkpoint")
    trained_config = output / config.name if (output / config.name).exists() else config
    s3, bucket = cr.r2_client(), os.environ["R2_BUCKET_NAME"]
    _upload(s3, bucket, [(checkpoints[-1], f"{prefix}/checkpoint/{checkpoints[-1].name}", "application/octet-stream"),
                         (trained_config, f"{prefix}/checkpoint/config_ft.yml", "text/yaml")])
    log(f"uploaded {checkpoints[-1].name} to {prefix}/checkpoint/")
    result = {"gpu": gpu_kind, "gpu_name": gpu_name, "physical_batch": batch, "amp": amp,
              "workers": workers, "prefetch": prefetch, "checkpoint": checkpoints[-1].name,
              "telemetry": monitor.summary(), "compile": False}
    samples_dir = output / "samples"
    sampled = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("sample_styletts2.py")), "--repo", str(STYLE_ROOT),
         "--config", str(trained_config), "--checkpoint", str(checkpoints[-1]), "--output", str(samples_dir)],
        timeout=3600,
    )
    if sampled.returncode == 0:
        files = [(p, f"{prefix}/samples/{p.name}", "audio/wav") for p in sorted(samples_dir.glob("*.wav"))]
        files.append((samples_dir / "samples.json", f"{prefix}/samples/samples.json", "application/json"))
        _upload(s3, bucket, files)
        result["samples"] = len(files) - 1
    else:
        result["samples_error"] = f"sample_styletts2.py exited {sampled.returncode}"
    s3.put_object(Bucket=bucket, Key=f"{prefix}/checkpoint/train_report.json",
                  Body=json.dumps(result, indent=2).encode(), ContentType="application/json")
    return result
