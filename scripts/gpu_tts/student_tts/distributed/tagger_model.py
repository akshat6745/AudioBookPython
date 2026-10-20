"""DeBERTa ensemble runtime. Imported only by the tag stage when a chapter needs tags."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .layout import TAGGER_MODEL, TAGGER_VERSION
from .tagger_logic import HEADS, add_softmax, labels_from_sums

_LOADED = None


def runtime_dir() -> Path:
    override = os.environ.get("AUDIOBOOK_TAGGER_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "audiobook-worker" / "emotion-tagger-v1" / TAGGER_VERSION


def _autocast(device):
    import torch

    return torch.autocast("cuda", dtype=torch.bfloat16, enabled=device.type == "cuda")


def _build_student(encoder, heads: dict):
    from torch import nn

    class Student(nn.Module):
        def __init__(self):
            super().__init__()
            self.encoder = encoder
            dim = encoder.config.hidden_size
            self.drop = nn.Dropout(0.1)
            self.heads = nn.ModuleDict({key: nn.Linear(dim, len(names)) for key, names in heads.items()})

        def forward(self, **enc):
            hidden = self.drop(self.encoder(**enc).last_hidden_state[:, 0])
            return {key: layer(hidden) for key, layer in self.heads.items()}

    return Student()


class Ensemble:
    def __init__(self, root: Path, device: str):
        import torch
        from safetensors.torch import load_model
        from transformers import AutoConfig, AutoModel, AutoTokenizer
        self.device = torch.device(device)
        self.models = []
        for name in TAGGER_MODEL.split("+"):
            folder = root / name
            cfg = json.loads((folder / "student_config.json").read_text())
            heads = cfg.get("labels") or HEADS
            hf = folder / "hf"
            encoder = AutoModel.from_config(AutoConfig.from_pretrained(hf, local_files_only=True))
            model = _build_student(encoder, heads)
            load_model(model, str(folder / "model.safetensors"), device="cpu")
            tok = AutoTokenizer.from_pretrained(hf, local_files_only=True)
            self.models.append((model.to(self.device).eval(), tok, int(cfg["max_len"]), heads))

    def tag_chapter(self, rows: list[dict], batch_size: int, device: str | None = None) -> list[dict]:
        import torch

        del device  # the ensemble is already on self.device
        total = None
        heads = self.models[0][3]
        for model, tok, max_len, _heads in self.models:
            probs = _softmax(model, tok, max_len, rows, self.device, batch_size)
            total = add_softmax(total, probs)
            del probs
        if self.device.type == "cuda":
            torch.cuda.empty_cache()
        elif self.device.type == "mps":
            torch.mps.empty_cache()
        return labels_from_sums(total, len(self.models), heads)


def _softmax(model, tok, max_len, rows, device, bs: int) -> dict[str, list[list[float]]]:
    import torch

    order = sorted(range(len(rows)), key=lambda i: len(rows[i]["text"]) + len(rows[i]["prev"]))
    width = {head: len(names) for head, names in HEADS.items()}
    buckets = {head: [[0.0] * width[head] for _ in rows] for head in HEADS}
    with torch.no_grad():
        for start in range(0, len(order), bs):
            idx = order[start:start + bs]
            enc = tok(
                [rows[j]["text"] for j in idx],
                [rows[j]["prev"] for j in idx],
                truncation="longest_first",
                max_length=max_len,
                padding=True,
                return_tensors="pt",
            ).to(device)
            enc.pop("token_type_ids", None)
            with _autocast(device):
                logits = model(**enc)
            for head in HEADS:
                probs = logits[head].float().softmax(-1).cpu()
                for offset, row_index in enumerate(idx):
                    buckets[head][row_index] = probs[offset].tolist()
    return buckets


def tag_chapter(rows: list[dict], batch_size: int, device: str) -> list[dict]:
    global _LOADED
    if _LOADED is None or _LOADED.device.type != device:
        _LOADED = Ensemble(runtime_dir(), device)
    return _LOADED.tag_chapter(rows, batch_size=batch_size, device=device)
