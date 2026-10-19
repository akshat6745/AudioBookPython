"""Synthesize eight post-training samples per voice using the final checkpoint."""
from __future__ import annotations

import argparse
import os
import sys
from collections import OrderedDict
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch
import torchaudio
import yaml
from phonemizer.backend import EspeakBackend

TEXTS = [
    "The morning light spread quietly across the empty road.",
    "I cannot believe it; after all this time, we finally found it!",
    "Get away from the door, now, before it is too late!",
    "She lowered her eyes and remembered everything they had lost.",
    "A laugh escaped him, bright and entirely unexpected.",
    "Whatever waits beyond that ridge, we will face it together.",
    "The silence made every shadow feel dangerously close.",
    "Perhaps the answer had always been hidden inside the question.",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--limit", type=int, default=len(TEXTS), help="sentences per voice (the CPU smoke uses 1)")
    args = parser.parse_args()
    repo, output = args.repo.resolve(), args.output.resolve()
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    from models import build_model, load_ASR_models, load_F0_models
    from utils import recursive_munch
    from text_utils import TextCleaner
    from Utils.PLBERT.util import load_plbert
    from Modules.diffusion.sampler import DiffusionSampler, ADPM2Sampler, KarrasSchedule

    config = yaml.safe_load(args.config.read_text())
    device = os.environ.get("SAMPLE_DEVICE") or ("cuda" if torch.cuda.is_available() else (
        "mps" if torch.backends.mps.is_available() else "cpu"
    ))
    model = build_model(
        recursive_munch(config["model_params"]),
        load_ASR_models(config["ASR_path"], config["ASR_config"]),
        load_F0_models(config["F0_path"]),
        load_plbert(config["PLBERT_dir"]),
    )
    checkpoint = args.checkpoint.resolve() if args.checkpoint else sorted(
        Path(config["log_dir"]).glob("epoch_2nd_*.pth")
    )[-1]
    params = torch.load(checkpoint, map_location="cpu")["net"]
    for key in model:
        if key in params:
            state = params[key]
            if state and next(iter(state)).startswith("module."):
                state = OrderedDict((name.removeprefix("module."), value) for name, value in state.items())
            model[key].load_state_dict(state, strict=False)
        model[key].eval().to(device)
    model_params = recursive_munch(config["model_params"])
    sampler = DiffusionSampler(
        model.diffusion.diffusion, sampler=ADPM2Sampler(),
        sigma_schedule=KarrasSchedule(sigma_min=0.0001, sigma_max=3.0, rho=9.0), clamp=False,
    )
    cleaner, phonemizer = TextCleaner(), EspeakBackend("en-us", preserve_punctuation=True, with_stress=True)
    to_mel = torchaudio.transforms.MelSpectrogram(n_mels=80, n_fft=2048, win_length=1200, hop_length=300)

    def mask(lengths):
        grid = torch.arange(lengths.max(), device=device).unsqueeze(0).expand(lengths.shape[0], -1)
        return grid + 1 > lengths.unsqueeze(1)

    def style(path: Path):
        audio, _ = librosa.load(path, sr=24000)
        audio, _ = librosa.effects.trim(audio, top_db=30)
        mel = (torch.log(1e-5 + to_mel(torch.from_numpy(audio).float()).unsqueeze(0)) + 4) / 4
        mel = mel.to(device)
        with torch.no_grad():
            return torch.cat([model.style_encoder(mel.unsqueeze(1)),
                              model.predictor_encoder(mel.unsqueeze(1))], dim=1)

    def infer(text: str, ref_s):
        phones = phonemizer.phonemize([text])[0]
        # Upstream splits punctuation off words with nltk.word_tokenize, which needs the punkt_tab download.
        for mark in ',.!?;:"':
            phones = phones.replace(mark, f" {mark} ")
        tokens = cleaner(" ".join(phones.split()))
        tokens.insert(0, 0)
        tokens = torch.LongTensor(tokens).to(device).unsqueeze(0)
        with torch.no_grad():
            lengths = torch.LongTensor([tokens.shape[-1]]).to(device)
            text_mask = mask(lengths)
            t_en = model.text_encoder(tokens, lengths, text_mask)
            bert = model.bert(tokens, attention_mask=(~text_mask).int())
            d_en = model.bert_encoder(bert).transpose(-1, -2)
            pred = sampler(noise=torch.randn((1, 1, 256), device=device), embedding=bert,
                           embedding_scale=1, features=ref_s, num_steps=5).squeeze(1)
            s = 0.7 * pred[:, 128:] + 0.3 * ref_s[:, 128:]
            ref = 0.3 * pred[:, :128] + 0.7 * ref_s[:, :128]
            d = model.predictor.text_encoder(d_en, s, lengths, text_mask)
            x, _ = model.predictor.lstm(d)
            duration = torch.round(torch.sigmoid(model.predictor.duration_proj(x)).sum(-1).squeeze()).clamp(min=1)
            align = torch.zeros(tokens.shape[-1], int(duration.sum()), device=device)
            cursor = 0
            for idx, frames in enumerate(duration):
                align[idx, cursor:cursor + int(frames)] = 1
                cursor += int(frames)
            en = d.transpose(-1, -2) @ align.unsqueeze(0)
            asr = t_en @ align.unsqueeze(0)
            if model_params.decoder.type == "hifigan":
                en = torch.cat([en[:, :, :1], en[:, :, :-1]], dim=-1)
                asr = torch.cat([asr[:, :, :1], asr[:, :, :-1]], dim=-1)
            f0, noise = model.predictor.F0Ntrain(en, s)
            return model.decoder(asr, f0, noise, ref).squeeze().cpu().numpy()[:-50]

    voice_dir = Path(__file__).resolve().parents[1] / "audiobook" / "voices"
    refs = {"narrator": voice_dir / "default_seed8.wav", "dialogue": voice_dir / "default_seed2.wav"}
    output.mkdir(parents=True, exist_ok=True)
    for voice in ("narrator", "dialogue"):
        ref_s = style(refs[voice])
        for idx, text in enumerate(TEXTS[:args.limit]):
            sf.write(output / f"{voice}-{idx + 1:02d}.wav", infer(text, ref_s), 24000, subtype="PCM_16")
    (output / "samples.json").write_text(
        __import__("json").dumps({"texts": TEXTS[:args.limit], "checkpoint": checkpoint.name}, indent=2)
    )


if __name__ == "__main__":
    main()
