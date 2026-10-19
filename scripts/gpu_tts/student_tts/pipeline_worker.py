"""Remote implementation for source selection, Higgs generation, and separate Whisper QA."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import calibration
import cloud_render as cr
import dataset
import higgs_format as hf
import prepare
import telemetry


def _id(row: dict) -> str:
    raw = json.dumps({k: row[k] for k in ("novel", "chapter", "paragraph", "chunk", "voice", "tagged_text")},
                     sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _write_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    if sr != 24000:
        raise ValueError(f"Higgs returned {sr} Hz, expected raw 24000 Hz")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, sr, 0, "NONE", "not compressed"))
        out.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def _source_rows(fetch_workers: int = 32) -> list[dict]:
    s3, bucket = cr.r2_client(), os.environ["R2_BUCKET_NAME"]
    rows = []
    for novel in cr.NOVELS:
        t0 = time.perf_counter()
        tag_key = f"{cr.ML_PREFIX}/tags/{novel}_{cr.TAG_MODEL}.jsonl"
        tags = [json.loads(line) for line in
                s3.get_object(Bucket=bucket, Key=tag_key)["Body"].read().decode().splitlines() if line.strip()]
        by_ch: dict[int, dict[int, dict]] = {}
        for tag in tags:
            by_ch.setdefault(int(tag["chapter"]), {})[int(tag["paragraph"])] = tag
        chapters = cr.d1_query(
            "SELECT chapter_number, r2_content_path FROM chapters "
            "WHERE novel_id = ? AND r2_content_path IS NOT NULL ORDER BY chapter_number", [novel],
        )
        chapters = [c for c in chapters if int(c["chapter_number"]) in by_ch]

        def fetch(chapter: dict) -> tuple[int, bytes]:
            body = s3.get_object(Bucket=bucket, Key=chapter["r2_content_path"])["Body"].read()
            return int(chapter["chapter_number"]), body

        with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
            for ch, text in pool.map(fetch, chapters):
                paragraphs = [p.strip() for p in cr.gw.decode_chapter_bytes(text).split("\n\n") if p.strip()]
                for idx, paragraph in enumerate(paragraphs):
                    if idx in by_ch[ch]:
                        rows.append({"novel": novel, "chapter": ch, "paragraph": idx,
                                     "text": paragraph, "tag": by_ch[ch][idx]})
        print(f"SELECT source {novel}: {len(chapters)} chapters, {len(rows)} rows so far, "
              f"{time.perf_counter() - t0:.0f}s", flush=True)
    return rows


def _run_rows(tts, rows: list[dict], raw_dir: Path, workers: int) -> tuple[list[dict], dict]:
    pending = len(rows)
    lock = threading.Lock()
    started = time.perf_counter()

    def one(row: dict) -> dict:
        nonlocal pending
        item = {**row, "id": _id(row), "generation_streams": workers}
        path = raw_dir / f"{item['id']}.wav"
        if path.exists():
            with lock:
                pending -= 1
            return {**item, "wav": str(path.relative_to(raw_dir.parent)), "cached": True}
        seed = int(item["id"][:8], 16)
        audio, sr, info = tts.generate(item["tagged_text"], seed, item["voice"])
        _write_wav(path, audio, sr)
        with lock:
            pending -= 1
        return {**item, "wav": str(path.relative_to(raw_dir.parent)),
                "duration_s": round(len(audio) / sr, 3), "generation": info}

    with telemetry.Monitor(lambda: pending) as monitor:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            completed = list(pool.map(one, rows))
    wall = time.perf_counter() - started
    audio_s = sum(r.get("duration_s", r["estimated_s"]) for r in completed)
    metrics = {**monitor.summary(), "streams": workers, "wall_s": round(wall, 2),
               "audio_s": round(audio_s, 2), "audio_s_per_wall_s": round(audio_s / max(wall, 0.001), 2)}
    print("HIGGS_THROUGHPUT " + json.dumps(metrics), flush=True)
    return completed, metrics


def _higgs_calibration(tts, rows: list[dict], raw_dir: Path, state: Path, gpu_name: str) -> tuple[int, list[dict]]:
    saved = calibration.load_choice(state, gpu_name, "higgs")
    if saved:
        return int(saved["chosen"]["setting"]), []
    traces, completed = [], []
    offset = 0
    for streams in (64, 72, 80):
        sample = rows[offset:offset + streams * 2]
        try:
            done, stats = _run_rows(tts, sample, raw_dir, streams)
            completed.extend(done)
            used = stats.get("peak_vram_mb", 0) / 1024
            measurement = calibration.Measurement(
                streams, True, stats["audio_s_per_wall_s"], used, 48.0,
                stats.get("median_utilization_pct", 0),
            )
        except (RuntimeError, subprocess.SubprocessError) as exc:
            measurement = calibration.Measurement(streams, False, 0, 48, 48, 0, error=type(exc).__name__)
        traces.append(measurement)
        offset += len(sample)
        # Higher widths are optional and bounded. A failed/near-full step ends calibration.
        if not measurement.stable or measurement.headroom_pct < 5:
            break
    chosen = calibration.choose_higgs(traces)
    calibration.save_choice(state, gpu_name, "higgs", chosen, traces)
    return chosen.setting, completed


def select_from_rows(work: Path, source_rows, quotas: dict[str, int] | None = None) -> dict:
    """Build a manifest from a source adapter (cloud in production, fixtures in preflight).

    source_rows may be a callable so a cached manifest skips fetching the sources entirely.
    """
    work = work.resolve()
    manifest_path = work / "request_manifest.jsonl"
    if manifest_path.exists():
        selected = [json.loads(line) for line in manifest_path.read_text().splitlines() if line.strip()]
        print(f"SELECT reusing cached manifest: {len(selected)} clips", flush=True)
        return {"manifest": dataset.summary(selected), "cached": True}
    rows = source_rows() if callable(source_rows) else source_rows
    selected = dataset.select_balanced(rows, quotas)
    planned = dataset.write_manifest(manifest_path, selected)
    return {"manifest": planned, "cached": False}


def select(work: Path) -> dict:
    """CPU-only: build or reload the balanced Higgs request manifest on the Volume."""
    result = select_from_rows(work, lambda: dataset.candidates(_source_rows()))
    planned = result["manifest"]
    if not 10 <= planned["total_estimated_hours"] <= 20:
        raise RuntimeError(f"selector produced {planned['total_estimated_hours']} h, outside the 10-20 h target")
    if planned["neutral_audio_pct"] > 50:
        raise RuntimeError(f"selector produced {planned['neutral_audio_pct']}% neutral audio")
    return result


def generate(work: Path, cache: Path) -> dict:
    work, cache = work.resolve(), cache.resolve()
    raw_dir = work / "raw"
    manifest_path = work / "request_manifest.jsonl"
    if not manifest_path.exists():
        raise RuntimeError("request_manifest.jsonl missing; run select_dataset first")
    selected = [json.loads(line) for line in manifest_path.read_text().splitlines() if line.strip()]
    generated_path = work / "generated.jsonl"
    if generated_path.exists():
        generated = [json.loads(line) for line in generated_path.read_text().splitlines() if line.strip()]
        if {r["id"] for r in generated} >= {_id(r) for r in selected} and all(
                (work / r["wav"]).exists() for r in generated):
            print(f"GENERATION reusing {len(generated)} clips already on the volume; Higgs not started", flush=True)
            return {"manifest": dataset.summary(selected), "generated": len(generated), "cached": True}
    cr.start_higgs(cache)
    try:
        import synth
        audiobook = Path(__file__).resolve().parents[1] / "audiobook"
        ref_text = (Path(__file__).resolve().parents[1] / "texts" / "passage.txt").read_text().strip()
        voices = {
            "narrator": (str(audiobook / "voices" / "default_seed8.wav"), ref_text),
            "dialogue": (str(audiobook / "voices" / "default_seed2.wav"), ref_text),
        }
        tts = synth.SglangHiggs(voices=voices)
        gpu_name = (telemetry.gpu_sample() or {}).get("gpu", "L40S")
        streams, calibration_rows = _higgs_calibration(
            tts, selected, raw_dir, cache / "calibration.json", gpu_name,
        )
        calibrated_ids = {r["id"] for r in calibration_rows}
        remaining = [r for r in selected if _id(r) not in calibrated_ids]
        done, metrics = _run_rows(tts, remaining, raw_dir, streams)
        generated = calibration_rows + done
        (work / "generated.jsonl").write_text("".join(json.dumps(r) + "\n" for r in generated))
    finally:
        subprocess.run(["bash", f"{cr.GPU_TTS}/higgs_v3/serve.sh", "stop"], check=False, timeout=60)
    return {"manifest": dataset.summary(selected), "generated": len(generated),
            "streams": streams, "telemetry": metrics}


def dataset_publication_plan(work: Path, prefix: str, accepted: list[dict]) -> list[tuple[Path, str, str]]:
    """Describe dataset uploads without performing network writes."""
    files = [
        (work / row["wav"], f"{prefix}/dataset/{row['wav']}", "audio/wav")
        for row in accepted
    ]
    files.extend(
        (work / name, f"{prefix}/dataset/{name}",
         "application/json" if name.endswith(".jsonl") else "text/plain")
        for name in ("metadata.jsonl", "train_list.txt", "val_list.txt", "OOD_texts.txt")
    )
    return files


QA_BATCH = 32  # clips per Whisper call; every clip is <= 16 s, inside Whisper's 30 s window


def _to_16k(clip: np.ndarray, sr: int) -> np.ndarray:
    if sr == 16000:
        return clip
    idx = np.linspace(0, len(clip) - 1, int(len(clip) * 16000 / sr))
    return np.interp(idx, np.arange(len(clip)), clip).astype(np.float32)


def _batch_transcriber():
    """fn(list of (audio, sr)) -> transcripts; one batched Whisper call on CUDA, clip by clip elsewhere."""
    import check_audio

    try:
        import torch
        cuda = torch.cuda.is_available()
    except ImportError:
        cuda = False
    if not cuda:
        one = check_audio.transcribe_setup()[1]
        return lambda clips: [one(audio, sr) for audio, sr in clips]
    from transformers import pipeline

    pipe = pipeline("automatic-speech-recognition", model=check_audio.WHISPER_TOKENIZER, device=0,
                    torch_dtype=torch.float16)

    def run(clips):
        inputs = [{"raw": _to_16k(audio, sr), "sampling_rate": 16000} for audio, sr in clips]
        return [o["text"] for o in pipe(inputs, batch_size=len(inputs), generate_kwargs={"language": "en"})]

    return run


def qa_and_publish(work: Path, prefix: str) -> dict:
    import check_audio
    import verify

    work = work.resolve()
    metadata = work / "metadata.jsonl"
    if metadata.exists():  # written only after a full transcription pass
        accepted = [json.loads(line) for line in metadata.read_text().splitlines() if line.strip()]
        summary = {"accepted": len(accepted),
                   "passed_hours": round(sum(r["duration_s"] for r in accepted) / 3600, 2), "cached": True}
        if all((work / name).exists() for name in ("train_list.txt", "val_list.txt")):
            print(f"QA reusing a finished pass: {len(accepted)} accepted clips and their lists", flush=True)
            return summary
        print(f"QA reusing {len(accepted)} accepted clips from metadata.jsonl; building the lists", flush=True)
        return _publish(work, prefix, accepted, summary)
    rows = [json.loads(line) for line in (work / "generated.jsonl").read_text().splitlines() if line.strip()]
    results_path = work / "qa_results.jsonl"  # one verdict per clip, appended as it is checked, so a rerun resumes
    done = {}
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["id"]] = r
    print(f"QA resuming with {len(done)}/{len(rows)} clips already checked", flush=True)
    accepted = [r for r in done.values() if r["qa"]["ok"]]
    rejected = [r for r in done.values() if not r["qa"]["ok"]]
    todo = [r for r in rows if r["id"] not in done]
    transcribe_batch = _batch_transcriber() if todo else None
    t0 = time.perf_counter()
    with telemetry.Monitor(lambda: len(rows) - len(accepted) - len(rejected)) as monitor, \
            open(results_path, "a") as results, ThreadPoolExecutor(max_workers=8) as io:
        for start in range(0, len(todo), QA_BATCH):
            batch = todo[start:start + QA_BATCH]
            clips = list(io.map(lambda r: check_audio.read_wav(work / r["wav"]), batch))
            heard = transcribe_batch(clips)
            for row, (audio, sr), text in zip(batch, clips, heard):
                verdict = verify.check_clip(row["text"], audio, sr, lambda *_, t=text: t)
                checked = {**row, "duration_s": round(len(audio) / sr, 3), "qa": verdict}
                (accepted if verdict["ok"] else rejected).append(checked)
                results.write(json.dumps(checked, ensure_ascii=False) + "\n")
            results.flush()
            print(f"QA {len(accepted) + len(rejected)}/{len(rows)} checked, {len(rejected)} rejected, "
                  f"{(len(accepted) + len(rejected) - len(done)) / max(time.perf_counter() - t0, 0.001):.1f} clips/s",
                  flush=True)
    wall = time.perf_counter() - t0
    passed_s = sum(r["duration_s"] for r in accepted)
    by_streams: dict[int, dict[str, float]] = {}
    for r in accepted + rejected:
        stats = by_streams.setdefault(int(r.get("generation_streams", 64)), {"passed_s": 0.0, "rejected_s": 0.0})
        stats["passed_s" if r["qa"]["ok"] else "rejected_s"] += r["duration_s"]
    if not accepted:
        raise RuntimeError("QA accepted no clips; nothing to train on")
    if passed_s < 10 * 3600:
        print(f"WARNING: QA passed only {passed_s / 3600:.2f} h (target 10-20 h); training on it anyway", flush=True)
    metadata.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in accepted))
    return _publish(work, prefix, accepted, {
        "accepted": len(accepted), "rejected": len(rejected), "passed_hours": round(passed_s / 3600, 2),
        "passed_audio_s_per_wall_s": round(passed_s / max(wall, 0.001), 2),
        "gpu": monitor.summary(), "acceptance_by_generation_streams": by_streams,
    })


def _publish(work: Path, prefix: str, accepted: list[dict], report: dict) -> dict:
    """StyleTTS2 lists on the Volume, then the accepted dataset and QA report to R2."""
    report = {**report, "lists": prepare.prepare_lists(work)}
    s3, bucket = cr.r2_client(), os.environ["R2_BUCKET_NAME"]
    uploads = dataset_publication_plan(work, prefix, accepted)
    t_up = time.perf_counter()
    with ThreadPoolExecutor(max_workers=32) as pool:  # ~5k small WAVs; one at a time took tens of GPU-minutes
        list(pool.map(lambda f: s3.upload_file(str(f[0]), bucket, f[1], ExtraArgs={"ContentType": f[2]}), uploads))
    print(f"QA uploaded {len(uploads)} dataset files in {time.perf_counter() - t_up:.0f}s", flush=True)
    s3.put_object(Bucket=bucket, Key=f"{prefix}/dataset/qa_report.json",
                  Body=json.dumps(report, indent=2).encode(), ContentType="application/json")
    return report
