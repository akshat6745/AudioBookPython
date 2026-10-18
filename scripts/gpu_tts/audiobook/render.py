#!/usr/bin/env python3
"""Inside the sandbox: chapters -> emotion tags -> Higgs-tagged text -> audio (tagged + untagged control).

  SANDBOX_CODE=<gpu_tts> SANDBOX_WORK=<run dir> bash orpheus/sandbox.sh python audiobook/render.py \
      --chapters-dir <dir of 00001.txt ...> --chapters 1 2 --ckpt <tagger dir> --out <run dir>

Writes per variant:
  paras/<variant>/<ch>/title.wav, 0.wav ... N-1.wav  one file per app paragraph (same layout as the offline bundle)
  manifest/<variant>/<ch>.json                       per paragraph: file, duration, text sha256, voices, tags
  wav/<variant>/<ch>.wav + segments/<variant>/<ch>.json  the same clips joined, for listening and Whisper checks
  text/<variant>/<ch>.txt (exact TTS input), plus tags/<ch>.jsonl and report.json.
Paragraph i is the backend's i-th paragraph (text.split("\n\n"), stripped, empties dropped); the app plays the
title as its index 0 and paragraph i as index i + 1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "emotion_eval")]
import higgs_format as hf  # noqa: E402
import postfx  # noqa: E402
import synth  # noqa: E402

PARA_GAP_S, CHUNK_GAP_S, BREAK_GAP_S, SKIP_S = 0.35, 0.12, 1.0, 0.3  # SKIP_S: silent clip for watermarks
VARIANTS = {"tagged": "every", "tagged_first": "first", "plain": None, "dual": "dual"}  # dual = narrator + dialogue voice


def paragraphs(path: Path) -> list[str]:
    """Exactly app/services/cloudflare_service.get_chapter_paragraphs, so indices match what the app shows."""
    return [p.strip() for p in path.read_text().split("\n\n") if p.strip()]


def seed_for(*parts) -> int:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def tag_chapters(texts: dict[int, list[str]], ckpt: str, out: Path) -> dict[int, list[dict]]:
    import torch
    import tag_novel

    rows = [{"chapter": ch, "paragraph": i, "text": t, "prev": ps[i - 1] if i else ""}
            for ch, ps in texts.items() for i, t in enumerate(ps)]
    dev = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    tags = tag_novel.tag_rows(rows, [ckpt], torch.device(dev), bs=256 if dev == "cuda" else 32)
    by_ch: dict[int, list[dict]] = {ch: [] for ch in texts}
    for r, t in zip(rows, tags):
        by_ch[r["chapter"]].append(t)
    (out / "tags").mkdir(parents=True, exist_ok=True)
    for ch, ts in by_ch.items():
        with open(out / "tags" / f"{ch:05d}.jsonl", "w") as f:
            f.writelines(json.dumps({"paragraph": i, **t}) + "\n" for i, t in enumerate(ts))
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    elif torch.backends.mps.is_available():
        torch.mps.empty_cache()  # free the tagger before Higgs loads
    return by_ch


def plan(ps: list[str], tags: list[dict], mode: str | None, cuts: hf.Cuts) -> list[list[tuple[str | None, str]]]:
    """(voice, TTS request) per paragraph, validated; mode None = untagged control, voice None = no reference."""
    out = []
    for p, t in zip(ps, tags):
        if hf.is_watermark(p):
            out.append([])
        elif mode == "dual":
            out.append([(voice, c) for voice, units in hf.format_dual(p, t, cuts) for c in hf.chunks(units)])
        else:
            units = hf.format_paragraph(p, t if mode else {}, cuts, mode or "every")
            out.append([(None, c) for c in hf.chunks(units)] if units else [])
    return out


def generate_cached(tts, text: str, seed: int, voice: str | None, cache: Path) -> tuple[np.ndarray, int, dict]:
    """Raw model output is kept, so post-processing changes re-stitch in seconds instead of re-generating."""
    key = tts.cache_key(text, seed, voice)
    path = cache / key[:2] / f"{key}.npz"
    if path.exists():
        z = np.load(path)
        return z["audio"], int(z["sr"]), {**json.loads(str(z["info"])), "total_s": 0.0, "cached": True}
    audio, sr, info = tts.generate(text, seed, voice)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, audio=audio.astype(np.float16), sr=sr, info=json.dumps(info))
    return audio, sr, info


def write_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def new_stats() -> dict:
    return {"requests": 0, "audio_s": 0.0, "compute_s": 0.0, "runaways": 0, "retried": 0, "fallback_plain": 0,
            "cached": 0, "bursts_dropped": 0, "check_retries": 0, "check_failed": []}


def merge_stats(into: dict, part: dict) -> None:
    for k, v in part.items():
        into[k] += v


def render(tts, novel: str, ch: int, variant: str, title: str, ps: list[str], reqs: list[list[tuple[str | None, str]]],
           tags: list[dict], out: Path, log, check=None, workers: int = 1, ext: str = ".mp3",
           ramp=None, qa_status: str | None = None) -> dict:
    """check: see speak(). stats["check_failed"] lists clips that never passed it, with their paragraph.

    workers > 1 generates that many paragraphs at once (the SGLang server batches concurrent requests); the
    output is identical to workers=1 because every clip's seed depends only on its position.
    ramp, while not settled, renders two waves at ramp.workers and then asks the ramp for the next width.
    qa_status is stored on the manifest (the Modal render uses "pending-whisper").
    """
    stats = new_stats()
    pdir = out / "paras" / variant / f"{ch:05d}"
    title_voice = "narrator" if variant == "dual" else None
    title_clip, sr = speak(tts, [(title_voice, hf.clean(title))], (novel, ch, "title"), 0.0, out, stats, check)
    for f in stats["check_failed"]:
        f["paragraph"] = "title"
    write_wav(pdir / "title.wav", title_clip, sr)

    def one(i: int) -> tuple[np.ndarray, int, dict]:
        part = new_stats()
        silence = SKIP_S if hf.is_watermark(ps[i]) else BREAK_GAP_S  # only used when the paragraph has nothing to say
        clip, sr = speak(tts, reqs[i], (novel, ch, i), silence, out, part, check)
        for f in part["check_failed"]:
            f["paragraph"] = i
        write_wav(pdir / f"{i}.wav", clip, sr)
        return clip, sr, part

    produced: dict[int, tuple[np.ndarray, int, dict]] = {}
    pending = list(range(len(reqs)))
    failures = 0
    while pending:
        width = ramp.workers if ramp else workers
        probing = ramp is not None and not ramp.settled
        # Probe with enough real paragraphs to fill the requested server width.
        batch = pending[: max(16, width * 2)] if probing else pending
        pending = pending[len(batch):]
        window = ramp.measure() if probing else None
        if window:
            window.start()
        try:
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max(1, width)) as pool:
                for i, result in zip(batch, pool.map(one, batch)):
                    produced[i] = result
            wall_s = time.perf_counter() - t0
        except Exception:
            if window:
                window.stop()
            failures += 1
            if not probing:
                raise
            old_width = ramp.workers
            ramp.observe(0.0, 1.0, errors=len(batch), clips=len(batch), sample=window.summary())
            if ramp.workers >= old_width:
                raise
            log(f"  {variant} ch{ch}: {old_width} streams failed; retrying at {ramp.workers}")
            pending = batch + pending
            continue
        if window:
            window.stop()
        if probing:
            audio_s = sum(len(produced[i][0]) / produced[i][1] for i in batch)
            ramp.observe(audio_s, wall_s, errors=0, clips=len(batch), sample=window.summary())

    t = len(title_clip) / sr
    pieces, segs, entries = [title_clip], [], []
    for i in range(len(reqs)):
        clip, sr, part = produced[i]
        merge_stats(stats, part)
        p, chunks = ps[i], reqs[i]
        spoken = len(clip) / sr - (PARA_GAP_S if chunks else len(clip) / sr)
        segs.append({"paragraph": i, "start": round(t, 3), "end": round(t + spoken, 3),
                     "requests": [c for _, c in chunks], "voices": [v for v, _ in chunks], "tags": tags[i]})
        entries.append({"index": i, "app_index": i + 1, "file": f"{i}{ext}", "duration_s": round(len(clip) / sr, 3),
                        "text_sha256": hashlib.sha256(p.encode()).hexdigest(), "silent": not chunks,
                        "voices": sorted({v for v, _ in chunks if v}), "tags": tags[i]})
        pieces.append(clip)
        t += len(clip) / sr
        if i % 10 == 0:
            width = ramp.workers if ramp else workers
            log(f"  {variant} ch{ch} paragraph {i + 1}/{len(reqs)}  audio {t / 60:5.1f} min  "
                f"streams {width}  gpu mem {tts.memory_gb()} GB")
    audio = np.concatenate(pieces)
    stats["audio_s"] = round(len(audio) / sr, 1)
    write_wav(out / "wav" / variant / f"{ch:05d}.wav", audio, sr)
    manifest = {
        "novel": novel, "chapter": ch, "tts": tts.name, "variant": variant, "sample_rate": sr,
        "title": {"text": title, "app_index": 0, "file": f"title{ext}", "duration_s": round(len(title_clip) / sr, 3)},
        "paragraphs": entries}
    if qa_status:
        manifest["qa"] = qa_status
    for sub, data in (("segments", segs), ("manifest", manifest)):
        (out / sub / variant).mkdir(parents=True, exist_ok=True)
        (out / sub / variant / f"{ch:05d}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    (out / "text" / variant).mkdir(parents=True, exist_ok=True)
    (out / "text" / variant / f"{ch:05d}.txt").write_text(
        "\n\n".join("\n".join(f"[{v}] {c}" if v else c for v, c in p) for p in reqs))
    return stats


CHECK_ATTEMPTS = 4  # first try + 3 new seeds; a clip fails the check ~1 time in 40, independently per seed


def generate_clip(tts, text: str, seed: int, voice: str | None, out: Path, stats: dict) -> tuple[np.ndarray, int]:
    audio, sr, info = generate_cached(tts, text, seed, voice, out / "cache")
    if info["runaway"] and hf.plain(text) != text:
        audio, sr, info2 = generate_cached(tts, hf.plain(text), seed, voice, out / "cache")
        stats["fallback_plain"] += 1
        info = {**info2, "total_s": info["total_s"] + info2["total_s"]}
    audio, fx = postfx.tidy(audio, sr)
    stats["bursts_dropped"] += bool(fx["dropped_burst_s"])
    stats["cached"] += bool(info.get("cached"))
    stats["requests"] += 1
    stats["compute_s"] += info["total_s"]
    stats["runaways"] += bool(info["runaway"])
    stats["retried"] += info["attempts"] > 1
    return audio, sr


def speak(tts, chunks: list[tuple[str | None, str]], seed_parts: tuple, silence_s: float, out: Path,
          stats: dict, check=None) -> tuple[np.ndarray, int]:
    """One app paragraph: its requests cleaned, level-matched and joined, ending with the paragraph pause.

    A paragraph with nothing to say (scene break, watermark) becomes silence_s of silence, so the app still has a file.
    check(text, audio, sr) -> verify.check_clip result: rejected clips are regenerated with new seeds, the best
    attempt is kept, and clips that never pass are appended to stats["check_failed"].
    """
    pieces, sr = [], 24000
    for j, (voice, text) in enumerate(chunks):
        seed = seed_for(*seed_parts, j)  # same seed for tagged and plain, so the A/B differs only in tags
        best = None
        for attempt in range(CHECK_ATTEMPTS if check else 1):
            audio, sr = generate_clip(tts, text, seed + 7919 * attempt, voice, out, stats)
            if not check:
                best = (audio, None)
                break
            verdict = check(hf.plain(text), audio, sr)
            if best is None or verdict["score"] < best[1]["score"]:
                best = (audio, verdict)
            if verdict["ok"]:
                break
            stats["check_retries"] += 1
        audio, verdict = best
        if verdict and not verdict["ok"]:
            stats["check_failed"].append({"voice": voice, "text": hf.plain(text), **verdict})
        pieces += [postfx.match_level(audio, sr), np.zeros(int(sr * CHUNK_GAP_S), np.float32)]
    if not pieces:
        return np.zeros(int(sr * silence_s), np.float32), sr
    pieces[-1] = np.zeros(int(sr * PARA_GAP_S), np.float32)
    return np.concatenate(pieces), sr


def clip_checker(transcribe=None):
    """verify.check_clip bound to a Whisper model, in the form speak()/render() take."""
    import check_audio
    import verify

    if transcribe is None:
        _, transcribe = check_audio.transcribe_setup()
    return lambda text, audio, sr: verify.check_clip(text, audio, sr, transcribe)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--novel", required=True)
    p.add_argument("--chapters-dir", type=Path, required=True)
    p.add_argument("--chapters", type=int, nargs="+", required=True)
    p.add_argument("--ckpt", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--variants", nargs="+", default=["tagged", "plain"], choices=list(VARIANTS))
    p.add_argument("--higgs", default="4bit", choices=["4bit", "8bit", "bf16"])
    p.add_argument("--backend", default="mlx", choices=["mlx", "sglang"])
    p.add_argument("--url", default="http://127.0.0.1:8000", help="SGLang server URL when --backend sglang")
    p.add_argument("--skip-tag", action="store_true", help="reuse existing out/tags/<ch>.jsonl from Phase 1")
    p.add_argument("--report", default="report.json")
    p.add_argument("--narrator", type=Path, help="reference wav for the narrator (dual)")
    p.add_argument("--dialogue", type=Path, help="reference wav for dialogue (dual)")
    p.add_argument("--ref-text", type=Path, help="transcript of both reference wavs")
    p.add_argument("--temperature", type=float, default=0.8, help="Higgs sampling temperature (see tune.py)")
    p.add_argument("--titles", type=Path, required=True, help='JSON {"<chapter>": "<title from D1>"}')
    p.add_argument("--check", action="store_true", help="Whisper-check every clip and regenerate rejects (verify.py)")
    a = p.parse_args()
    t0 = time.perf_counter()

    def log(m: str) -> None:
        print(f"[{time.perf_counter() - t0:6.0f}s] {m}", flush=True)

    texts = {ch: paragraphs(a.chapters_dir / f"{ch:05d}.txt") for ch in a.chapters}
    titles = json.loads(a.titles.read_text())
    titles = {ch: f"Chapter {ch}: {titles[str(ch)]}" for ch in a.chapters}  # what the app reads at index 0
    if a.skip_tag and all((a.out / "tags" / f"{ch:05d}.jsonl").exists() for ch in a.chapters):
        log(f"reusing pre-computed tags for chapters {a.chapters}")
        tags = {ch: [json.loads(ln) for ln in (a.out / "tags" / f"{ch:05d}.jsonl").read_text().splitlines() if ln.strip()]
                for ch in a.chapters}
    elif all(any(hf.parse_annotated_paragraph(p) for p in texts[ch]) for ch in a.chapters):
        log(f"using inline <|voice:...|emotion:...|> tags already embedded in chapter files {a.chapters}")
        tags = {}
        for ch in a.chapters:
            ch_tags = []
            for p_idx, p_txt in enumerate(texts[ch]):
                inline_spans = hf.parse_annotated_paragraph(p_txt)
                if inline_spans:
                    spans_meta = [{"voice": v, "text": s, **t} for v, s, t in inline_spans]
                    dlg = [s for s in spans_meta if s["voice"] == "dialogue"]
                    prim = dlg[0] if dlg else spans_meta[0]
                    ch_tags.append({
                        "paragraph": p_idx,
                        "emotion": prim["emotion"],
                        "style": prim["style"],
                        "sfx": prim["sfx"],
                        "confidence": prim["confidence"],
                        "style_conf": prim["style_conf"],
                        "sfx_conf": prim["sfx_conf"],
                        "spans": spans_meta,
                    })
                else:
                    ch_tags.append({
                        "paragraph": p_idx,
                        "emotion": "neutral", "style": "none", "sfx": "none",
                        "confidence": 1.0, "style_conf": 1.0, "sfx_conf": 1.0,
                    })
            tags[ch] = ch_tags
    else:
        log(f"{sum(map(len, texts.values()))} paragraphs in chapters {a.chapters}; tagging")
        tags = tag_chapters(texts, a.ckpt, a.out)
    cuts = hf.Cuts()
    plans = {(v, ch): plan(texts[ch], tags[ch], VARIANTS[v], cuts) for v in a.variants for ch in a.chapters}
    log("formatted + validated every request; loading Higgs")
    voices = None
    if "dual" in a.variants:
        if not (a.narrator and a.dialogue and a.ref_text):
            p.error("dual needs --narrator, --dialogue and --ref-text")
        ref_text = a.ref_text.read_text().strip()
        voices = {"narrator": (str(a.narrator), ref_text), "dialogue": (str(a.dialogue), ref_text)}
    tts = synth.backend(a.backend, variant=a.higgs, url=a.url, voices=voices, sampling={"temperature": a.temperature})
    check = clip_checker() if a.check else None
    report = {"novel": a.novel, "tts": tts.name, "sampling": tts.sampling, "cuts": cuts.__dict__, "chapters": {}}
    for ch in a.chapters:
        ts = tags[ch]
        tagged = [t for t in ts if t["emotion"] != "neutral" and t["confidence"] >= cuts.emotion]
        report["chapters"][ch] = {
            "paragraphs": len(ts), "emotion_tagged": len(tagged),
            "emotions": {e: sum(t["emotion"] == e for t in tagged) for e in sorted({t["emotion"] for t in tagged})},
            "style_tagged": sum(t["style"] != "none" and t["style_conf"] >= cuts.style for t in ts),
            "sfx_tagged": sum(t["sfx"] != "none" and t["sfx_conf"] >= cuts.sfx for t in ts),
            "variants": {}}
        for v in a.variants:
            s = render(tts, a.novel, ch, v, titles[ch], texts[ch], plans[(v, ch)], ts, a.out, log, check)
            s["rtf"] = round(s["compute_s"] / max(s["audio_s"], 1e-9), 2)
            report["chapters"][ch]["variants"][v] = s
            log(f"ch{ch} {v}: {s['audio_s'] / 60:.1f} min audio, rtf {s['rtf']}, runaways {s['runaways']}, "
                f"fallback {s['fallback_plain']}, check retries {s['check_retries']}, "
                f"still failing {len(s['check_failed'])}")
            (a.out / a.report).write_text(json.dumps(report, indent=2))
    log("done")


if __name__ == "__main__":
    main()
