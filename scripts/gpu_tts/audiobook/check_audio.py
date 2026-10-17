#!/usr/bin/env python3
"""Inside the sandbox: transcribe every rendered paragraph with Whisper and flag problems.

  SANDBOX_CODE=<gpu_tts> SANDBOX_WORK=<run dir> bash orpheus/sandbox.sh python audiobook/check_audio.py --out <run dir>

Flags a paragraph when a tag is spoken aloud ("emotion", "sfx", tag names), when word error rate vs the book text
is above --max-wer (dropped or garbled words), or when its audio is far longer than the text needs.
Writes check.json next to report.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import higgs_format as hf  # noqa: E402

WHISPER = "mlx-community/whisper-large-v3-turbo"
WHISPER_TOKENIZER = "openai/whisper-large-v3-turbo"  # the MLX repo ships weights only
INTERJECTION = re.compile(r"^(?:a?(?:ha)+|(?:he)+h?|heh|haa+h|phew|ahem|sniff|sob|a+h+|argh+)$")  # sounds, not words
TAG_WORDS = {"emotion", "sfx", "prosody", "style"} | {w for names in hf.CATALOG.values() for n in names for w in n.split("_")}


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))


def wer(ref: list[str], hyp: list[str]) -> float:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[-1] / max(len(ref), 1)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768, w.getframerate()


def transcribe_setup():
    """Return (Whisper name, transcribe_fn(audio_float32, sr) -> str) using mlx_audio on Mac or transformers on CUDA/CPU."""
    try:
        from mlx_audio.stt.utils import load_model, resample_audio
        from transformers import WhisperProcessor

        asr = load_model(WHISPER)
        asr._processor = WhisperProcessor.from_pretrained(WHISPER_TOKENIZER)
        return WHISPER, lambda clip, sr: asr.generate(
            resample_audio(clip, sr, 16000), language="en", condition_on_previous_text=False
        ).text
    except ImportError:
        import torch
        from transformers import pipeline

        dev = 0 if torch.cuda.is_available() else -1
        dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        pipe = pipeline("automatic-speech-recognition", model=WHISPER_TOKENIZER, device=dev, torch_dtype=dtype)

        def _resample(clip: np.ndarray, sr: int) -> np.ndarray:
            if sr == 16000:
                return clip
            idx = np.linspace(0, len(clip) - 1, int(len(clip) * 16000 / sr))
            return np.interp(idx, np.arange(len(clip)), clip).astype(np.float32)

        return WHISPER_TOKENIZER, lambda clip, sr: pipe(
            {"raw": _resample(clip, sr), "sampling_rate": 16000},
            generate_kwargs={"language": "en"},
        )["text"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--max-wer", type=float, default=0.15)
    p.add_argument("--variants", nargs="+", help="only these (results merge into an existing check.json)")
    a = p.parse_args()
    wname, transcribe = transcribe_setup()
    old = a.out / "check.json"
    result = {"whisper": wname, "max_wer": a.max_wer,
              "variants": json.loads(old.read_text())["variants"] if old.exists() and a.variants else {}}
    for seg_file in sorted((a.out / "segments").glob("*/*.json")):
        variant, ch = seg_file.parent.name, seg_file.stem
        if a.variants and variant not in a.variants:
            continue
        audio, sr = read_wav(a.out / "wav" / variant / f"{ch}.wav")
        rows, flagged = [], []
        for s in json.loads(seg_file.read_text()):
            text = hf.plain(" ".join(s["requests"]))
            ref = [w for w in words(text) if not INTERJECTION.match(w)]
            if not ref:
                continue
            heard = transcribe(audio[int(s["start"] * sr):int(s["end"] * sr)], sr)
            hyp = [w for w in words(heard) if not INTERJECTION.match(w)]
            row = {"paragraph": s["paragraph"], "wer": round(wer(ref, hyp), 3), "heard": heard.strip(),
                   "tags_spoken": sorted(TAG_WORDS & set(hyp) - set(ref)),
                   "too_long": (s["end"] - s["start"]) > 2.5 * max(1.0, len(text) / 15)}
            rows.append(row)
            if row["wer"] > a.max_wer or row["tags_spoken"] or row["too_long"]:
                flagged.append(row)
        result["variants"].setdefault(variant, {})[ch] = {
            "paragraphs": len(rows), "mean_wer": round(float(np.mean([r["wer"] for r in rows])), 3) if rows else None,
            "flagged": flagged, "rows": rows}
        print(f"{variant:13} ch{ch}: {len(rows)} paragraphs, mean WER "
              f"{result['variants'][variant][ch]['mean_wer']}, flagged {len(flagged)} "
              f"(tags spoken {sum(bool(r['tags_spoken']) for r in flagged)}, too long {sum(r['too_long'] for r in flagged)})",
              flush=True)
    (a.out / "check.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
