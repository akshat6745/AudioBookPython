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
CALIBRATION_BUDGET_S = 15 * 60
HEAVY_BUDGET_S = 7 * 60  # heavy probes first (the run cannot start without one); plain gets the rest
CALIBRATION_STEPS = 25
# ~11 h of audio x 6 epochs ~= 66 audio-hours seen, more than upstream's 1 h x 50 epochs. 15 epochs projected
# to 15-30 L40S hours, past the 23 h function cap.
EPOCHS = int(os.environ.get("STUDENT_TTS_EPOCHS", "6"))
TRAIN_TIMEOUT_S = 23 * 3600


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


def training_phases(epochs: int) -> list[tuple[str, int, int]]:
    """(phase, first epoch, end epoch): plain until joint_epoch, then heavy (SLM adversarial) to the end."""
    joint = prepare.phase_epochs(epochs)[1]
    return [("plain", 0, joint), ("heavy", joint, epochs)]


def next_epoch(output: Path) -> int:
    """Epoch the next train_finetune.py run starts at: one past the newest epoch_2nd_<epoch>.pth."""
    checkpoints = sorted(output.glob("epoch_2nd_*.pth"))
    return int(checkpoints[-1].stem.rsplit("_", 1)[1]) + 1 if checkpoints else 0


def training_estimate(data: Path, choices: dict[str, dict], epochs: int, start_epoch: int = 0) -> dict:
    """Projected wall time of the remaining epochs from each phase's calibration step time, logged before the
    paid full run starts. The plain probe runs diffusion from step 0, so epoch 0 (no diffusion) is overestimated."""
    clips = sum(1 for line in (data / "train_list.txt").read_text().splitlines() if line.strip())
    phases, total = {}, 0.0
    for phase, first, end in training_phases(epochs):
        batch, step_s = int(choices[phase]["setting"]), float(choices[phase].get("step_s", 0))
        steps = clips // batch  # StyleTTS2's train loader drops the last partial batch
        remaining = max(0, end - max(first, start_epoch))
        hours = remaining * steps * step_s / 3600
        phases[phase] = {"epochs": [first, end], "remaining_epochs": remaining, "batch": batch,
                         "steps_per_epoch": steps, "step_s": step_s, "hours": round(hours, 2)}
        total += hours
    return {"clips": clips, "epochs": epochs, "start_epoch": start_epoch, "phases": phases,
            "projected_hours": round(total, 2)}


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


def _calibrate_phase(phase: str, candidates: tuple[int, ...], deadline: float, data: Path, output: Path,
                     state: Path, gpu_name: str, total_vram: float, default_amp: str,
                     fallback: dict | None = None) -> dict | None:
    """Probe one phase's batches upward; None when nothing stable was measured (budget, OOM or timeout).
    With a fallback (the heavy choice, safe for plain since plain is a subset of its losses) a timeout or a
    spent budget ends probing instead of failing the run."""
    kind = calibration.phase_kind(phase)
    saved = calibration.load_choice(state, gpu_name, kind)
    if saved:
        log(f"reusing saved {phase} calibration for {gpu_name}: {json.dumps(saved['chosen'])}")
        return saved["chosen"] | {"amp": saved.get("amp", "off")}
    trace: list[calibration.Measurement] = []
    config = output.parent / f"calibration-{phase}.yml"
    amp = default_amp
    for batch in candidates:
        remaining = int(deadline - time.monotonic())
        if remaining < 120:
            log(f"{phase} calibration budget spent before batch {batch}")
            break
        # This phase's losses run from step 0 so the probe sees its heaviest step's memory.
        prepare.write_config(config, data, output / "calibration-output", batch, epochs=1, probe=phase)
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
        log("CALIBRATION " + json.dumps({"phase": phase, **m.__dict__, "amp": amp,
                                         "headroom_pct": round(m.headroom_pct, 1),
                                         "slm_steps": metrics.get("slm_steps")}))
        if logs.rstrip().endswith("TIMEOUT") and (fallback or any(t.stable for t in trace)):
            log(f"{phase} batch {batch} probe ran past its time limit; keeping what was measured before it")
            break
        _raise_unless_oom(code, stable, oom, logs, batch)
        if not stable or m.headroom_pct < 7.5:
            break  # larger batches only use more memory
    good = [m for m in trace if m.stable and m.headroom_pct >= 7.5]
    if not good and fallback:
        log(f"no {phase} batch above {fallback['setting']} measured; using the heavy choice for {phase} too")
        good = [calibration.Measurement(**{k: v for k, v in fallback.items() if k != "amp"})]
        trace.append(good[0])
    if not good:
        return None
    chosen = calibration.choose_training(good)
    calibration.save_choice(state, gpu_name, kind, chosen, trace, extra={"amp": amp})
    log(f"CALIBRATION_CHOICE phase={phase} batch={chosen.setting} amp={amp} examples/s={chosen.throughput} "
        f"step_s={chosen.step_s} vram={chosen.vram_used_gb}/{chosen.vram_total_gb} GB util={chosen.utilization_pct}%")
    return chosen.__dict__ | {"amp": amp}


def _calibrate(data: Path, output: Path, state: Path, gpu_name: str, total_vram: float,
               default_amp: str) -> dict[str, dict]:
    """Batch per training phase, persisted under styletts2-v2-<phase>; the v1 single choice is ignored."""
    start = time.monotonic()
    heavy = _calibrate_phase("heavy", calibration.PHASE_BATCHES["heavy"], start + HEAVY_BUDGET_S, data, output,
                             state, gpu_name, total_vram, default_amp)
    if heavy is None:
        raise RuntimeError("batch 2 ran out of memory or lacked headroom at max_len 400 with the SLM losses on; "
                           "StyleTTS2 cannot train below batch 2, so lower max_len instead")
    # Plain runs a subset of heavy's losses, so it fits at least heavy's batch; only larger ones are worth probing.
    plain_candidates = tuple(b for b in calibration.PHASE_BATCHES["plain"] if b > heavy["setting"])
    plain = _calibrate_phase("plain", plain_candidates, start + CALIBRATION_BUDGET_S, data, output, state,
                             gpu_name, total_vram, heavy["amp"], fallback=heavy)
    return {"plain": plain, "heavy": heavy}


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


def train_phases(work: Path, output: Path, config: Path, pretrained: Path, choices: dict[str, dict], amp: str,
                 epochs: int = EPOCHS, timeout_s: int = TRAIN_TIMEOUT_S, loaders: dict | None = None) -> dict:
    """Plain epochs at the plain batch, then the SLM epochs resumed from the last checkpoint at the heavy batch.

    The resume restores AdamW moments too (load_only_params false). That is safe across a batch change because
    the pinned train_finetune.py never steps its OneCycleLR schedulers (div_factor = final_div_factor = 1, and
    MultiOptimizer saves no scheduler state): the LR is constant, so a different steps_per_epoch cannot
    reschedule it. A retried container re-enters at the phase of the newest checkpoint; an OOM resumes the same
    phase one supported batch smaller. loaders overrides (workers, prefetch) per phase.
    """
    report = {}
    for phase, first, end in training_phases(epochs):
        start = next_epoch(output)
        if start >= end:
            log(f"{phase} epochs [{first}, {end}) already trained")
            continue
        batch = int(choices[phase]["setting"])
        underfed = choices[phase].get("utilization_pct", 100) < 85
        workers, prefetch = (loaders or {}).get(phase) or ((12, 6) if underfed else (8, 4))
        while True:
            prepare.write_config(config, work, output, batch, epochs=epochs, pretrained=pretrained, stop_epoch=end)
            log(f"TRAIN_START phase={phase} epochs=[{next_epoch(output)}, {end}) batch={batch} amp={amp} "
                f"workers={workers} prefetch={prefetch}")
            with telemetry.Monitor(lambda: 1, interval_s=30) as monitor:
                code, _, logs = _run(config, 0, timeout_s, amp, workers, prefetch)
            if code == 0:
                break
            if not _is_oom(logs):
                raise RuntimeError(f"StyleTTS2 {phase} phase exited {code}")
            batch = calibration.next_smaller_batch(batch)
            workers, prefetch = (loaders or {}).get(phase) or (8, 2)
            log(f"OOM in the {phase} phase; resuming from the last checkpoint with batch {batch}")
        if next_epoch(output) < end:
            raise RuntimeError(f"{phase} phase exited 0 but the newest checkpoint is before epoch {end - 1}")
        report[phase] = {"batch": batch, "workers": workers, "prefetch": prefetch, "telemetry": monitor.summary()}
    return report


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
    choices = _calibrate(work, output, cache / "calibration.json", gpu_name, total_vram, "off")
    amp = choices["heavy"]["amp"]
    log("TRAIN_ESTIMATE " + json.dumps(training_estimate(work, choices, EPOCHS, next_epoch(output))))
    config = output.parent / "config_ft.yml"  # train_finetune.py writes the trained copy to output/config_ft.yml
    phases = train_phases(work, output, config, pretrained, choices, amp)

    checkpoints = sorted(output.glob("epoch_2nd_*.pth"))
    if not checkpoints:
        raise RuntimeError("training completed without a checkpoint")
    trained_config = output / config.name if (output / config.name).exists() else config
    s3, bucket = cr.r2_client(), os.environ["R2_BUCKET_NAME"]
    _upload(s3, bucket, [(checkpoints[-1], f"{prefix}/checkpoint/{checkpoints[-1].name}", "application/octet-stream"),
                         (trained_config, f"{prefix}/checkpoint/config_ft.yml", "text/yaml")])
    log(f"uploaded {checkpoints[-1].name} to {prefix}/checkpoint/")
    batches = {p: int(c["setting"]) for p, c in choices.items()} | {p: r["batch"] for p, r in phases.items()}
    result = {"gpu": gpu_kind, "gpu_name": gpu_name, "amp": amp, "phases": phases, "physical_batch": batches,
              "checkpoint": checkpoints[-1].name, "compile": False}
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
