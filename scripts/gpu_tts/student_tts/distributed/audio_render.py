"""Chapter waveform render. Imported only after the audio stage's tag preflight."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import uuid
import wave
from pathlib import Path

import numpy as np

from . import layout
from .audio_publish import destination_for, publish_chapter_audio, recover_if_elected
from .batching import call_reducing, group_batches
from .profile import save_effective
from .style_plan import (
    BREAK_GAP_S,
    CHUNK_GAP_S,
    PARA_GAP_S,
    plan_paragraph,
    seed_for,
    silence_seconds,
    split_long,
)
from .textutil import raw_paragraphs, sha256_text
from .validate import classify_audio, classify_tag


def _postfx():
    import sys

    root = str(Path(__file__).resolve().parents[2] / "audiobook")
    if root not in sys.path:
        sys.path.insert(0, root)
    import postfx

    return postfx


def _write_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def _loudnorm(wav: Path) -> str:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(wav), "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    start = proc.stderr.rindex("{")
    measured = float(__import__("json").loads(proc.stderr[start:proc.stderr.index("}", start) + 1])["input_i"])
    return f"volume={layout.TARGET_LUFS - measured:.2f}dB,alimiter=limit={layout.LIMITER}:level=false"


def _encode(wav: Path, dest: Path, audio_filter: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), "-af", audio_filter,
            "-ac", "1", "-ar", str(layout.SAMPLE_RATE), "-codec:a", "libopus", "-vbr", "on",
            "-application", "audio", "-b:a", layout.BITRATE, str(dest),
        ],
        check=True,
    )


class Speaker:
    def __init__(self, student, bank, batch: int, token_budget: int):
        self.student = student
        self.bank = bank
        self.batch = batch
        self.token_budget = token_budget

    def jobs_for(self, chunks: list[dict], key: tuple) -> list[dict]:
        jobs = []
        ip = getattr(self.bank, "inference_params", {}) or {}
        for index, chunk in enumerate(chunks):
            is_nar = chunk["voice"] == "narrator"
            alpha = float(ip.get("alpha", layout.NARRATOR_ALPHA)) if is_nar else layout.DIALOGUE_ALPHA
            beta = float(ip.get("beta", layout.NARRATOR_BETA)) if is_nar else layout.DIALOGUE_BETA
            esc = float(ip.get("embedding_scale", layout.NARRATOR_EMBEDDING_SCALE)) if is_nar else layout.DIALOGUE_EMBEDDING_SCALE
            f0_sc = float(ip.get("f0_scale", ip.get("f0_mult", layout.NARRATOR_F0_SCALE))) if is_nar else layout.DIALOGUE_F0_SCALE
            spd = float(ip.get("speed", ip.get("length_scale", 1.0))) if is_nar else 1.0
            t_db = float(ip.get("narrator_target_db", layout.NARRATOR_TARGET_DB)) if is_nar else float(ip.get("dialogue_target_db", layout.DIALOGUE_TARGET_DB))
            entry = self.bank.pick(chunk["voice"], chunk["emotion"], *key)
            tokens = self.student.tokens(chunk["text"])
            parts = [(chunk["text"], tokens)]
            if len(tokens) > layout.MAX_PHONEME_TOKENS:
                parts = [(text, self.student.tokens(text)) for text in split_long(chunk["text"])]
            for part, (text, toks) in enumerate(parts):
                seed = seed_for(*key, index) if part == 0 else seed_for(*key, index, part)
                jobs.append({
                    "chunk": index, "part": part, "parts": len(parts), "text": text,
                    "voice": chunk["voice"], "alpha": alpha, "beta": beta, "embedding_scale": esc,
                    "f0_scale": f0_sc, "speed": spd, "target_db": t_db, "tokens": toks, "entry": entry, "seed": seed,
                })
        return jobs

    def render(self, jobs: list[dict]) -> list[np.ndarray]:
        import torch

        postfx = _postfx()
        groups = group_batches([len(job["tokens"]) for job in jobs], self.batch, self.token_budget)
        out: list = [None] * len(jobs)
        for group in groups:
            ref = torch.cat([self.student.bank_style(self.bank.vectors, jobs[index]["entry"]["index"]) for index in group])
            audios = self.student.infer_batch(
                [jobs[index]["tokens"] for index in group], ref, [jobs[index]["seed"] for index in group],
                alphas=[jobs[index]["alpha"] for index in group],
                betas=[jobs[index]["beta"] for index in group],
                embedding_scales=[jobs[index]["embedding_scale"] for index in group],
                f0_scales=[jobs[index]["f0_scale"] for index in group],
                speeds=[jobs[index].get("speed", 1.0) for index in group],
            )
            for index, audio in zip(group, audios):
                out[index] = postfx.tidy(audio, layout.SAMPLE_RATE)[0]
        return out

    def stitch(self, chunks, jobs, audios, silence_s: float):
        postfx = _postfx()
        pieces, styles = [], []
        for job, audio in zip(jobs, audios):
            if job["part"] == 0:
                chunk, entry = chunks[job["chunk"]], job["entry"]
                styles.append({
                    "voice": chunk["voice"], "emotion": chunk["emotion"], "style_index": entry["index"],
                    "fallback": entry["fallback"], **({"split_parts": job["parts"]} if job["parts"] > 1 else {}),
                })
            t_db = job.get("target_db", layout.NARRATOR_TARGET_DB if chunks[job["chunk"]]["voice"] == "narrator" else layout.DIALOGUE_TARGET_DB)
            leveled = postfx.match_level(audio, layout.SAMPLE_RATE, target_db=t_db)
            if t_db > -20.0:
                peak = float(np.abs(leveled).max())
                if peak > 0.88:
                    leveled = (0.88 * np.tanh(leveled / 0.88)).astype(np.float32)
            pieces += [leveled, np.zeros(int(layout.SAMPLE_RATE * CHUNK_GAP_S), np.float32)]
        if not pieces:
            return np.zeros(int(layout.SAMPLE_RATE * silence_s), np.float32), styles
        pieces[-1] = np.zeros(int(layout.SAMPLE_RATE * PARA_GAP_S), np.float32)
        return np.concatenate(pieces), styles


def _load_models(device: str):
    from .bootstrap import cache_dir
    from .style_plan import StyleBank
    from .tts_engine import BatchStudent

    cache = cache_dir() / layout.STYLETTS_RUNTIME / layout.STYLETTS_VERSION
    override = __import__("os").environ.get("AUDIOBOOK_STYLETTS_ROOT")
    root = Path(override) if override else cache
    student = BatchStudent(
        device,
        root / "StyleTTS2",
        root / "checkpoint" / "epoch_2nd_00005.pth",
        root / "config.runtime.yml",
    )
    bank = StyleBank(root / "style_bank" / "bank.npz", root / "style_bank" / "bank.json")
    return student, bank


_MODELS = None


def render_and_publish(store, source, chapter, *, batch, device, worker_id, profile_file, token_budget, log, claim=None, now_fn=None, replace_stale: bool = False) -> None:
    tag_row = classify_tag(store, source, chapter, now=0 if now_fn is None else now_fn())
    audio_row = classify_audio(store, source, chapter, now=0 if now_fn is None else now_fn(), tag_row=tag_row)
    overwrite = bool(replace_stale and audio_row["status"] in {"stale", "legacy_valid"})
    dest = destination_for(
        audio_row["status"] if audio_row["status"] == "stale" else "missing",
        source["slug"],
        chapter,
        replace_stale=replace_stale,
    )
    if audio_row["status"] == "stale":
        log(f"chapter {chapter} canonical audio is stale; writing {dest}")
    elif audio_row["status"] == "legacy_valid" and replace_stale:
        log(f"chapter {chapter} legacy audio is being replaced with {layout.VOICE_BANK}; writing {dest}")
    elif audio_row["status"] in {"valid", "legacy_valid"}:
        log(f"chapter {chapter} audio already valid")
        return
    if not overwrite and recover_if_elected(store, dest):
        log(f"chapter {chapter} finished from an elected audio attempt")
        return
    spec = next(item for item in source["chapters"] if int(item["chapter"]) == int(chapter))
    text = store.get_bytes(spec["key"]).decode()
    if sha256_text(text) != spec["sha256"]:
        raise RuntimeError(f"source hash mismatch for chapter {chapter}")
    titles = __import__("json").loads(store.get_bytes(layout.titles_key(source["slug"])).decode())
    title = f"Chapter {chapter}: {titles.get(str(chapter), f'Chapter {chapter}')}"
    tags = [__import__("json").loads(line) for line in store.get_bytes(_tag_blob(store, source, chapter)).decode().splitlines() if line.strip()]
    paragraphs = raw_paragraphs(text)
    if len(tags) != len(paragraphs):
        raise RuntimeError(f"chapter {chapter} tags {len(tags)} != paragraphs {len(paragraphs)}")

    global _MODELS
    if _MODELS is None:
        _MODELS = _load_models(device)
    student, bank = _MODELS

    def attempt(width: int):
        if claim is not None and now_fn is not None and not claim.heartbeat(now_fn()):
            raise RuntimeError(f"lost the audio lease for chapter {chapter}")
        return _generate(student, bank, source["slug"], chapter, title, paragraphs, tags, width, token_budget)

    def on_reduce(width, _exc):
        log(f"chapter {chapter} audio batch reduced to {width} after OOM")
        from .deviceutil import empty_cache

        empty_cache(device)

    (manifest, files, _width), effective, _tried = call_reducing(batch, attempt, on_reduce=on_reduce)
    manifest["source_sha256"] = spec["sha256"]
    manifest["tag_manifest_sha256"] = tag_row.get("manifest_sha256")
    manifest["tagger_version"] = layout.TAGGER_MODEL
    manifest["batch_requested"] = batch
    manifest["batch_effective"] = effective
    manifest["worker_id"] = worker_id
    publish_chapter_audio(
        store, slug=source["slug"], chapter=chapter, dest_prefix=dest, files=files,
        manifest=manifest, attempt_id=uuid.uuid4().hex, overwrite=overwrite,
    )
    save_effective("audio", device, f"{layout.STYLETTS_RUNTIME}@{layout.STYLETTS_VERSION}", effective, profile_file)
    log(f"chapter {chapter} audio published batch={effective} files={len(files)}")


def _tag_blob(store, source, chapter: int) -> str:
    import json

    from .layout import tag_manifest_key

    manifest = json.loads(store.get_bytes(tag_manifest_key(source["slug"], chapter)).decode())
    return manifest["jsonl_key"]


def _generate(student, bank, slug, chapter, title, paragraphs, tags, batch, token_budget) -> tuple[dict, dict[str, bytes], int]:
    speaker = Speaker(student, bank, batch, token_budget)
    postfx = _postfx()
    del postfx
    plans = [([{"voice": "narrator", "emotion": "neutral", "text": title}], (slug, chapter, "title"), 0.0)]
    for index, (paragraph, tag) in enumerate(zip(paragraphs, tags)):
        plans.append((plan_paragraph(paragraph, tag), (slug, chapter, index), silence_seconds(paragraph)))
    jobs = [speaker.jobs_for(chunks, key) for chunks, key, _silence in plans]
    flat = [job for group in jobs for job in group]
    audios = speaker.render(flat) if flat else []
    clips, cursor = [], 0
    for (chunks, _key, silence), group in zip(plans, jobs):
        clips.append(speaker.stitch(chunks, group, audios[cursor:cursor + len(group)], silence))
        cursor += len(group)
    work = Path(__import__("tempfile").mkdtemp(prefix=f"audiobook-ch{chapter}-"))
    try:
        title_clip = clips[0][0]
        _write_wav(work / "title.wav", title_clip, layout.SAMPLE_RATE)
        pieces = [title_clip]
        entries = []
        for index, (paragraph, tag) in enumerate(zip(paragraphs, tags)):
            clip, styles = clips[index + 1]
            _write_wav(work / f"{index}.wav", clip, layout.SAMPLE_RATE)
            entries.append({
                "index": index, "app_index": index + 1, "file": f"{index}.opus",
                "duration_s": round(len(clip) / layout.SAMPLE_RATE, 3),
                "text_sha256": hashlib.sha256(paragraph.encode()).hexdigest(),
                "silent": not plans[index + 1][0],
                "voices": sorted({chunk["voice"] for chunk in plans[index + 1][0]}),
                "tags": tag, "styles": styles,
            })
            pieces.append(clip)
        audio = np.concatenate(pieces)
        chapter_wav = work / "chapter.wav"
        _write_wav(chapter_wav, audio, layout.SAMPLE_RATE)
        audio_filter = _loudnorm(chapter_wav)
        opus_dir = work / "opus"
        for wav in sorted((work).glob("*.wav")):
            if wav.name == "chapter.wav":
                continue
            _encode(wav, opus_dir / f"{wav.stem}.opus", audio_filter)
        _encode(chapter_wav, work / "chapter.opus", audio_filter)
        files = {}
        for entry in [{"file": "title.opus"}, *entries, {"file": "chapter.opus"}]:
            path = (work / "chapter.opus") if entry["file"] == "chapter.opus" else opus_dir / entry["file"]
            raw = path.read_bytes()
            files[entry["file"]] = raw
            if entry["file"] != "chapter.opus":
                entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        chapter_raw = files["chapter.opus"]
        ip = getattr(bank, "inference_params", {}) or {}
        voice_bank = str(ip.get("slug") or getattr(layout, "VOICE_BANK", "kokoro_af_heart_d018"))
        manifest = {
            "novel": slug, "chapter": chapter, "tts": "styletts2-student-eye-v1", "variant": "student",
            "voice_bank": voice_bank,
            "sample_rate": layout.SAMPLE_RATE,
            "title": {"text": title, "app_index": 0, "file": "title.opus", "duration_s": round(len(title_clip) / layout.SAMPLE_RATE, 3),
                      "bytes": len(files["title.opus"]), "sha256": hashlib.sha256(files["title.opus"]).hexdigest()},
            "paragraphs": entries,
            "chapter_file": {"file": "chapter.opus", "bytes": len(chapter_raw), "sha256": hashlib.sha256(chapter_raw).hexdigest(),
                             "duration_s": round(len(audio) / layout.SAMPLE_RATE, 3)},
            "qa": {"whisper": "not_run", "format": f"opus {layout.BITRATE}", "loudness": audio_filter},
        }
        # title bytes were set above inside the loop only for entries. Set title explicitly.
        manifest["title"]["bytes"] = len(files["title.opus"])
        manifest["title"]["sha256"] = hashlib.sha256(files["title.opus"]).hexdigest()
        return manifest, files, batch
    finally:
        shutil.rmtree(work, ignore_errors=True)
        student.sync()
