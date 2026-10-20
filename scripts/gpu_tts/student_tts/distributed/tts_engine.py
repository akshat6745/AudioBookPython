"""StyleTTS2 student loader and length-masked batch inference.

Imported only when an audio worker renders a chapter. Training-only StyleTTS imports are
patched out of ``utils.py`` by bootstrap. WavLM / LibriTTS bases are not loaded.
"""

from __future__ import annotations

import math
import os
import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
import yaml
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

EPS = 1e-5


def sync_device(device: str) -> None:
    if device == "mps":
        torch.mps.synchronize()
    elif device.startswith("cuda"):
        torch.cuda.synchronize()


class Student:
    def __init__(self, device: str, styletts_root: Path, checkpoint: Path, config_path: Path):
        self.device = device
        self.styletts_root = styletts_root
        cwd = os.getcwd()
        os.chdir(styletts_root)
        sys.path.insert(0, str(styletts_root))
        try:
            from models import build_model, load_ASR_models, load_F0_models
            from text_utils import TextCleaner
            from utils import recursive_munch
            from Utils.PLBERT.util import load_plbert
            from Modules.diffusion.sampler import ADPM2Sampler, DiffusionSampler, KarrasSchedule

            config = yaml.safe_load(config_path.read_text())
            self._config = config
            model = build_model(
                recursive_munch(config["model_params"]),
                load_ASR_models(config["ASR_path"], config["ASR_config"]),
                load_F0_models(config["F0_path"]),
                load_plbert(config["PLBERT_dir"]),
            )
        finally:
            os.chdir(cwd)
        params = torch.load(checkpoint, map_location="cpu", weights_only=False)["net"]
        for key in model:
            if key in params:
                state = params[key]
                if state and next(iter(state)).startswith("module."):
                    state = OrderedDict((name.removeprefix("module."), value) for name, value in state.items())
                model[key].load_state_dict(state, strict=False)
            model[key].eval().to(device)
        del params
        self.model = model
        self.hifigan = recursive_munch(config["model_params"]).decoder.type == "hifigan"
        self.sampler = DiffusionSampler(
            model.diffusion.diffusion,
            sampler=ADPM2Sampler(),
            sigma_schedule=KarrasSchedule(sigma_min=0.0001, sigma_max=3.0, rho=9.0),
            clamp=False,
        )
        self.cleaner = TextCleaner()
        from phonemizer.backend import EspeakBackend

        self.phonemizer = EspeakBackend("en-us", preserve_punctuation=True, with_stress=True)
        self._styles: dict[int, torch.Tensor] = {}

    def sync(self) -> None:
        sync_device(self.device)

    def tensor(self, ref_s: np.ndarray) -> torch.Tensor:
        return torch.from_numpy(np.asarray(ref_s, np.float32)).reshape(1, 256).to(self.device)

    def bank_style(self, vectors: np.ndarray, index: int) -> torch.Tensor:
        cached = self._styles.get(index)
        if cached is None:
            cached = self._styles[index] = self.tensor(vectors[index])
        return cached

    def tokens(self, text: str) -> list[int]:
        phones = self.phonemizer.phonemize([text])[0]
        for mark in ',.!?;:"':
            phones = phones.replace(mark, f" {mark} ")
        tokens = self.cleaner(" ".join(phones.split()))
        tokens.insert(0, 0)
        return tokens

    def _rng_state(self):
        if self.device == "mps":
            return torch.mps.get_rng_state()
        if self.device.startswith("cuda"):
            return torch.cuda.get_rng_state()
        return torch.get_rng_state()

    def _set_rng_state(self, state) -> None:
        if self.device == "mps":
            torch.mps.set_rng_state(state)
        elif self.device.startswith("cuda"):
            torch.cuda.set_rng_state(state)
        else:
            torch.set_rng_state(state)


class Lengths:
    def __init__(self, frames: torch.Tensor, total: int, device):
        self.frames, self.total, self.device, self._masks = frames, total, device, {}

    def mask(self, factor: int) -> torch.Tensor:
        cached = self._masks.get(factor)
        if cached is None:
            grid = torch.arange(self.total * factor)
            cached = (grid[None] < (self.frames * factor)[:, None]).float()[:, None].to(self.device)
            self._masks[factor] = cached
        return cached


def _adain(norm, x, s, mask):
    hidden = norm.fc(s)
    gamma, beta = torch.chunk(hidden.view(hidden.size(0), hidden.size(1), 1), chunks=2, dim=1)
    count = mask.sum(-1, keepdim=True)
    mean = (x * mask).sum(-1, keepdim=True) / count
    centered = x - mean
    var = (centered * centered * mask).sum(-1, keepdim=True) / count
    return (1 + gamma) * (centered * torch.rsqrt(var + EPS)) + beta


def _adain_resblk(block, x, s, lens: Lengths, factor: int):
    up = block.upsample_type != "none"
    mask_in = lens.mask(factor)
    factor_out = factor * 2 if up else factor
    mask_out = lens.mask(factor_out)
    hidden = block.actv(_adain(block.norm1, x, s, mask_in))
    if up:
        hidden = block.pool(hidden * mask_in)
    hidden = block.conv1(hidden * mask_out)
    hidden = block.actv(_adain(block.norm2, hidden, s, mask_out))
    hidden = block.conv2(hidden * mask_out)
    return (hidden + block._shortcut(x)) / math.sqrt(2), factor_out


def _adain_resblock1(block, x, s, mask):
    for conv1, conv2, norm1, norm2, alpha1, alpha2 in zip(
        block.convs1, block.convs2, block.adain1, block.adain2, block.alpha1, block.alpha2
    ):
        hidden = _adain(norm1, x, s, mask)
        hidden = hidden + (1 / alpha1) * (torch.sin(alpha1 * hidden) ** 2)
        hidden = conv1(hidden * mask)
        hidden = _adain(norm2, hidden, s, mask)
        hidden = hidden + (1 / alpha2) * (torch.sin(alpha2 * hidden) ** 2)
        hidden = conv2(hidden * mask)
        x = hidden + x
    return x


def _packed_lstm(lstm, x, lengths):
    out, _state = lstm(pack_padded_sequence(x, lengths, batch_first=True, enforce_sorted=False))
    return pad_packed_sequence(out, batch_first=True, total_length=x.shape[1])[0]


class BatchStudent(Student):
    """Several chunks per call. Each item keeps the samples of its own seeded call."""

    def _transformer(self, x, time, emb, feats, keep, count):
        net = self.model.diffusion.diffusion.net
        mapping = net.get_mapping(time, feats)
        hidden = torch.cat([x.expand(-1, emb.size(1), -1), emb], dim=-1)
        mapping = mapping.unsqueeze(1).expand(-1, emb.size(1), -1)
        bias = torch.zeros(keep.shape, device=keep.device).masked_fill(~keep, float("-inf"))[:, None, None, :]
        for block in net.blocks:
            hidden = hidden + mapping
            attention = block.attention
            query_in = attention.norm(hidden, feats)
            context = attention.norm_context(hidden, feats)
            query = attention.to_q(query_in)
            key, value = torch.chunk(attention.to_kv(context), chunks=2, dim=-1)
            base = attention.attention
            batch, time_steps, _width = query.shape
            query, key, value = (item.view(batch, time_steps, base.num_heads, -1).transpose(1, 2) for item in (query, key, value))
            sim = torch.einsum("... n d, ... m d -> ... n m", query, key) * base.scale + bias
            out = torch.einsum("... n m, ... m d -> ... n d", sim.softmax(dim=-1), value)
            hidden = base.to_out(out.transpose(1, 2).reshape(batch, time_steps, -1)) + hidden
            hidden = block.feed_forward(hidden) + hidden
        hidden = (hidden * keep[..., None]).sum(dim=1, keepdim=True) / count[:, None, None]
        return net.to_out(hidden).transpose(-1, -2)

    def _diffusion(self, noises, emb, feats, keep, count):
        kd = self.model.diffusion.diffusion
        adpm2 = self.sampler.sampler
        sigmas = self.sampler.sigma_schedule(5, self.device)

        def denoise(x, sigma):
            c_skip, c_out, c_in, c_noise = kd.get_scale_weights(torch.full((x.shape[0],), sigma).to(self.device))
            return c_skip * x + c_out * self._transformer(c_in * x, c_noise, emb, feats, keep, count)

        x = sigmas[0] * noises[0]
        for index in range(4):
            sigma, sigma_next = sigmas[index], sigmas[index + 1]
            sigma_up, sigma_down, sigma_mid = adpm2.get_sigmas(sigma, sigma_next)
            slope = (x - denoise(x, sigma)) / sigma
            x_mid = x + slope * (sigma_mid - sigma)
            slope_mid = (x_mid - denoise(x_mid, sigma_mid)) / sigma_mid
            x = x + slope_mid * (sigma_down - sigma) + noises[index + 1] * sigma_up
        return x

    def _source(self, f0, rand_ini, noise):
        source = self.model.decoder.generator.m_source
        sine = source.l_sin_gen
        harmonics = f0 * torch.arange(1, sine.harmonic_num + 2, dtype=torch.float32, device=f0.device)[None, None]
        rad = (harmonics / sine.sampling_rate) % 1
        rad[:, 0, :] = rad[:, 0, :] + rand_ini
        rad = F.interpolate(rad.transpose(1, 2), scale_factor=1 / sine.upsample_scale, mode="linear").transpose(1, 2)
        phase = torch.cumsum(rad, dim=1) * 2 * np.pi
        phase = F.interpolate(phase.transpose(1, 2) * sine.upsample_scale, scale_factor=sine.upsample_scale, mode="linear").transpose(1, 2)
        sines = torch.sin(phase) * sine.sine_amp
        voiced = (f0 > sine.voiced_threshold).float()
        waves = sines * voiced + (voiced * sine.noise_std + (1 - voiced) * sine.sine_amp / 3) * noise
        return source.l_tanh(source.l_linear(waves))

    def _generator(self, x, style, f0_curve, rand_ini, noise, lens: Lengths):
        gen = self.model.decoder.generator
        f0 = gen.f0_upsamp(f0_curve[:, None]).transpose(1, 2)
        harmonic = self._source(f0, rand_ini, noise).transpose(1, 2) * lens.mask(600)
        factor = 2
        for index, scale in enumerate((10, 5, 3, 2)):
            x = x + (1 / gen.alphas[index]) * (torch.sin(gen.alphas[index] * x) ** 2)
            mask = lens.mask(factor * scale)
            source = _adain_resblock1(gen.noise_res[index], gen.noise_convs[index](harmonic), style, mask)
            x = gen.ups[index](x * lens.mask(factor)) + source
            factor *= scale
            total = None
            for kernel in range(gen.num_kernels):
                y = _adain_resblock1(gen.resblocks[index * gen.num_kernels + kernel], x, style, mask)
                total = y if total is None else total + y
            x = total / gen.num_kernels
        x = x + (1 / gen.alphas[-1]) * (torch.sin(gen.alphas[-1] * x) ** 2)
        return torch.tanh(gen.conv_post(x * lens.mask(factor)))

    def infer_batch(self, tokens: list[list[int]], ref_s: torch.Tensor, seeds: list[int], per_item: str | None = "f0n") -> list[np.ndarray]:
        model, device = self.model, self.device
        batch = len(tokens)
        n_cpu = torch.tensor([len(item) for item in tokens])
        width = int(n_cpu.max())
        ids = torch.zeros(batch, width, dtype=torch.long)
        for index, item in enumerate(tokens):
            ids[index, : len(item)] = torch.tensor(item)
        ids, lengths = ids.to(device), n_cpu.to(device)
        text_mask = (torch.arange(width)[None] >= n_cpu[:, None]).to(device)
        keep = ~text_mask
        noises, states = [[] for _ in range(5)], []
        for seed in seeds:
            torch.manual_seed(seed)
            for noise in noises:
                noise.append(torch.randn((1, 1, 256), device=device))
            states.append(self._rng_state())
        noises = [torch.cat(item) for item in noises]
        with torch.no_grad():
            text_encoding = model.text_encoder(ids, lengths, text_mask)
            bert = model.bert(ids, attention_mask=keep.int())
            duration_encoding = model.bert_encoder(bert).transpose(-1, -2)
            pred = self._diffusion(noises, bert, ref_s, keep, lengths.float()).squeeze(1)
            prosody = 0.7 * pred[:, 128:] + 0.3 * ref_s[:, 128:]
            acoustic = 0.3 * pred[:, :128] + 0.7 * ref_s[:, :128]
            duration_hidden = model.predictor.text_encoder(duration_encoding, prosody, lengths, text_mask)
            lstm_out = _packed_lstm(model.predictor.lstm, duration_hidden, n_cpu)
            duration = torch.round(torch.sigmoid(model.predictor.duration_proj(lstm_out)).sum(-1)).clamp(min=1)
            frames_cpu = duration.to("cpu", torch.long)
            frames = torch.stack([frames_cpu[index, : n_cpu[index]].sum() for index in range(batch)])
            total = int(frames.max())
            align = torch.zeros(batch, width, total)
            for index in range(batch):
                item_frames = frames_cpu[index, : n_cpu[index]]
                align[index, torch.repeat_interleave(torch.arange(int(n_cpu[index])), item_frames), torch.arange(int(frames[index]))] = 1
            align = align.to(device)
            encoded = duration_hidden.transpose(-1, -2) @ align
            asr = text_encoding @ align
            if self.hifigan:
                encoded = torch.cat([encoded[:, :, :1], encoded[:, :, :-1]], dim=-1)
                asr = torch.cat([asr[:, :, :1], asr[:, :, :-1]], dim=-1)
            lens = Lengths(frames, total, device)
            predictor = model.predictor
            if per_item == "f0n":
                waves = []
                for index in range(batch):
                    frames_i = int(frames[index])
                    f0, noise = predictor.F0Ntrain(encoded[index:index + 1, :, :frames_i], prosody[index:index + 1])
                    self._set_rng_state(states[index])
                    waves.append(model.decoder(asr[index:index + 1, :, :frames_i], f0, noise, acoustic[index:index + 1])[0, 0])
                return [wave.cpu().numpy()[:-50] for wave in waves]
            hidden = _packed_lstm(predictor.shared, encoded.transpose(-1, -2), frames).transpose(-1, -2)
            f0, noise, factor = hidden, hidden, 1
            for block in predictor.F0:
                f0, factor = _adain_resblk(block, f0, prosody, lens, factor)
            f0 = predictor.F0_proj(f0).squeeze(1) * lens.mask(2)[:, 0]
            factor = 1
            for block in predictor.N:
                noise, factor = _adain_resblk(block, noise, prosody, lens, factor)
            noise = predictor.N_proj(noise).squeeze(1) * lens.mask(2)[:, 0]
            if per_item == "decoder":
                waves = []
                for index in range(batch):
                    frames_i = int(frames[index])
                    self._set_rng_state(states[index])
                    waves.append(model.decoder(
                        asr[index:index + 1, :, :frames_i], f0[index:index + 1, : 2 * frames_i],
                        noise[index:index + 1, : 2 * frames_i], acoustic[index:index + 1],
                    )[0, 0])
                return [wave.cpu().numpy()[:-50] for wave in waves]
            rand_ini = torch.zeros(batch, 9, device=device)
            noise_draw = torch.zeros(batch, 600 * total, 9, device=device)
            for index in range(batch):
                self._set_rng_state(states[index])
                rand_ini[index] = torch.rand(1, 9, device=device)[0]
                noise_draw[index, : 600 * int(frames[index])] = torch.randn(9, 600 * int(frames[index]), device=device).T
            rand_ini[:, 0] = 0
            decoder = model.decoder
            f0_conv = decoder.F0_conv(f0.unsqueeze(1))
            n_conv = decoder.N_conv(noise.unsqueeze(1))
            hidden, factor = _adain_resblk(decoder.encode, torch.cat([asr, f0_conv, n_conv], dim=1), acoustic, lens, 1)
            asr_res = decoder.asr_res(asr)
            residual = True
            for block in decoder.decode:
                if residual:
                    hidden = torch.cat([hidden, asr_res, f0_conv, n_conv], dim=1)
                hidden, factor = _adain_resblk(block, hidden, acoustic, lens, factor)
                if block.upsample_type != "none":
                    residual = False
            wave = self._generator(hidden, acoustic, f0, rand_ini, noise_draw, lens)[:, 0].cpu().numpy()
        return [wave[index, : 600 * int(frames[index])][:-50] for index in range(batch)]
