"""Tests for the standalone GPU library worker & presigned bundle pipeline."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import sys
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parent / "emotion_eval")]

import gpu_library_worker as gw  # noqa: E402


def _make_wav_bytes(sr: int = 24000, duration_s: float = 0.5) -> bytes:
    import io
    buf = io.BytesIO()
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    pcm = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _sample_bundle(tmp_path: Path) -> dict:
    wav_raw = _make_wav_bytes()
    wav_sha = hashlib.sha256(wav_raw).hexdigest()
    wav_b64 = base64.b64encode(wav_raw).decode("ascii")
    return {
        "version": 1,
        "phase": "both",
        "expires_hours": 24.0,
        "settings": {
            "variant": "dual",
            "narrator": "default_seed8",
            "dialogue": "default_seed2",
            "temperature": 0.8,
            "bitrate": "64k",
            "model": "deberta3large_teacher2",
        },
        "model_files": {
            "model.safetensors": "https://r2.example.com/model.safetensors",
            "config.json": "https://r2.example.com/config.json",
            "SHA256SUMS": "https://r2.example.com/SHA256SUMS",
        },
        "voices": {
            "narrator_name": "default_seed8",
            "dialogue_name": "default_seed2",
            "ref_text": "The old lighthouse stood at the edge of the world.",
            "wav_b64": {"narrator": wav_b64, "dialogue": wav_b64},
            "sha256": {"narrator": wav_sha, "dialogue": wav_sha},
        },
        "novels": [
            {
                "novel": "shadow-slave",
                "tags_already_in_r2": False,
                "get_tags_url": None,
                "put_tags_url": "https://r2.example.com/put/tags/shadow-slave.jsonl",
                "chapters": [
                    {
                        "chapter": 1,
                        "title": "Nightmare Begins",
                        "get_text_url": "https://r2.example.com/get/ch1.txt.gz",
                        "paragraph_count": 2,
                        "r2_prefix": "audio/shadow-slave/higgs_v3-default_seed8-default_seed2/chapter_1",
                        "put_mp3_urls": {
                            "title.mp3": "https://r2.example.com/put/ch1/title.mp3",
                            "0.mp3": "https://r2.example.com/put/ch1/0.mp3",
                            "1.mp3": "https://r2.example.com/put/ch1/1.mp3",
                        },
                        "put_manifest_url": "https://r2.example.com/put/ch1/manifest.json",
                    }
                ],
            }
        ],
    }


def test_ensure_voices_verifies_sha256(tmp_path: Path):
    bundle = _sample_bundle(tmp_path)
    n_wav, d_wav, ref = gw.ensure_voices(bundle, tmp_path)
    assert n_wav.exists() and d_wav.exists() and ref.exists()
    assert json.loads((tmp_path / "voices" / "voices.json").read_text()) == {
        "narrator": "default_seed8",
        "dialogue": "default_seed2",
    }

    bad = _sample_bundle(tmp_path)
    bad["voices"]["sha256"]["narrator"] = "0" * 64
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        gw.ensure_voices(bad, tmp_path / "bad")


def test_ensure_tagger_model_verifies_checksums(tmp_path: Path):
    bundle = _sample_bundle(tmp_path)
    weights = b"fake-safetensors-weights"
    cfg = b'{"base": "microsoft/deberta-v3-large"}'
    sums = (
        f"{hashlib.sha256(weights).hexdigest()}  model.safetensors\n"
        f"{hashlib.sha256(cfg).hexdigest()}  config.json\n"
    ).encode()
    store = {
        "https://r2.example.com/model.safetensors": weights,
        "https://r2.example.com/config.json": cfg,
        "https://r2.example.com/SHA256SUMS": sums,
    }
    with patch.object(gw, "http_get", side_effect=lambda u, **_: store[u]):
        ckpt = gw.ensure_tagger_model(bundle, tmp_path)
    assert (ckpt / ".verified").exists()
    assert (ckpt / "model.safetensors").read_bytes() == weights


def test_phase1_and_phase2_end_to_end_mocked(tmp_path: Path):
    pytest.importorskip("tag_novel", reason="needs the emotion tagger code (emotion_eval)")
    bundle = _sample_bundle(tmp_path)
    ch_text = 'The cold wind blew.\n\n"Get ready!" he shouted.'
    ch_gz = gzip.compress(ch_text.encode("utf-8"))
    weights = b"weights"
    cfg = b'{"base": "microsoft/deberta-v3-large"}'
    sums = (
        f"{hashlib.sha256(weights).hexdigest()}  model.safetensors\n"
        f"{hashlib.sha256(cfg).hexdigest()}  config.json\n"
    ).encode()

    get_store = {
        "https://r2.example.com/model.safetensors": weights,
        "https://r2.example.com/config.json": cfg,
        "https://r2.example.com/SHA256SUMS": sums,
        "https://r2.example.com/get/ch1.txt.gz": ch_gz,
    }
    put_store: dict[str, tuple[bytes, str]] = {}

    def fake_get(url: str, **_) -> bytes:
        return get_store[url]

    def fake_put(url: str, data: bytes, content_type: str, **_) -> None:
        put_store[url] = (data, content_type)

    fake_tags = [
        {"emotion": "fear", "style": "none", "sfx": "none", "confidence": 0.91, "style_conf": 0.99, "sfx_conf": 0.95},
        {"emotion": "anger", "style": "shouting", "sfx": "none", "confidence": 0.95, "style_conf": 0.98, "sfx_conf": 0.92},
    ]

    class FakeTts:
        name = "higgs_v3-sglang"
        sampling = {"temperature": 0.8, "top_k": 50}

        def memory_gb(self) -> float:
            return 11.5

        def cache_key(self, text: str, seed: int, voice: str | None) -> str:
            return hashlib.sha256(f"{voice}:{text}:{seed}".encode()).hexdigest()

        def generate(self, text: str, seed: int, voice: str | None = None):
            sr = 24000
            t = np.linspace(0, 0.6, int(sr * 0.6), endpoint=False)
            audio = (0.15 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
            return audio, sr, {"audio_s": 0.6, "total_s": 0.15, "runaway": False, "attempts": 1}

    with patch.object(gw, "http_get", side_effect=fake_get), \
         patch.object(gw, "http_put", side_effect=fake_put), \
         patch("tag_novel.tag_rows", return_value=fake_tags), \
         patch("synth.backend", return_value=FakeTts()):
        gw.run_phase1_tagging(bundle, tmp_path, None)
        assert "https://r2.example.com/put/tags/shadow-slave.jsonl" in put_store
        tag_lines = put_store["https://r2.example.com/put/tags/shadow-slave.jsonl"][0].decode().splitlines()
        assert len(tag_lines) == 2

        gw.run_phase2_audio(
            bundle, tmp_path, url="http://127.0.0.1:8000", backend_name="sglang",
            higgs_variant="4bit", skip_whisper=True, keep_files=False, novels_filter=None,
        )

    assert "https://r2.example.com/put/ch1/title.mp3" in put_store
    assert "https://r2.example.com/put/ch1/0.mp3" in put_store
    assert "https://r2.example.com/put/ch1/1.mp3" in put_store
    assert "https://r2.example.com/put/ch1/manifest.json" in put_store

    manifest = json.loads(put_store["https://r2.example.com/put/ch1/manifest.json"][0].decode())
    assert manifest["novel"] == "shadow-slave"
    assert manifest["chapter"] == 1
    assert manifest["voices"] == {"narrator": "default_seed8", "dialogue": "default_seed2"}
    assert manifest["title"]["file"] == "title.mp3"
    assert len(manifest["paragraphs"]) == 2
    assert manifest["paragraphs"][0]["app_index"] == 1
    assert manifest["paragraphs"][1]["app_index"] == 2
    assert "sha256" in manifest["paragraphs"][0] and "bytes" in manifest["paragraphs"][0]


def test_phase1_single_archive_mode(tmp_path: Path):
    pytest.importorskip("tag_novel", reason="needs the emotion tagger code (emotion_eval)")
    import io
    import tarfile
    import torch

    bundle = _sample_bundle(tmp_path)
    rows = [
        {"novel": "shadow-slave", "chapter": 1, "paragraph": 0, "text": "The cold wind blew.", "prev": ""},
        {"novel": "shadow-slave", "chapter": 1, "paragraph": 1, "text": '"Get ready!" he shouted.', "prev": "The cold wind blew."},
    ]
    gz_bytes = gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"))
    in_path = tmp_path / "paragraphs_input.jsonl.gz"
    out_tar = tmp_path / "tags_output.tar.gz"
    out_novel = tmp_path / "shadow-slave.jsonl"
    in_path.write_bytes(gz_bytes)

    weights = b"weights"
    cfg = b'{"base": "microsoft/deberta-v3-large", "max_len": 256}'
    sums = (
        f"{hashlib.sha256(weights).hexdigest()}  model.safetensors\n"
        f"{hashlib.sha256(cfg).hexdigest()}  config.json\n"
    ).encode()
    m_dir = tmp_path / "m"
    m_dir.mkdir()
    (m_dir / "model.safetensors").write_bytes(weights)
    (m_dir / "config.json").write_bytes(cfg)
    (m_dir / "SHA256SUMS").write_bytes(sums)
    bundle["model_files"] = {
        k: f"file://{m_dir / k}" for k in ("model.safetensors", "config.json", "SHA256SUMS")
    }
    out_ch1 = tmp_path / "chapter_1.txt.gz"
    bundle["archive"] = {
        "input_sha256": hashlib.sha256(gz_bytes).hexdigest(),
        "get_paragraphs_url": f"file://{in_path}",
        "put_archive_url": f"file://{out_tar}",
        "put_novel_tag_urls": {"shadow-slave": f"file://{out_novel}"},
        "put_chapter_text_urls": {"shadow-slave": {"1": f"file://{out_ch1}"}},
    }

    # 3 spans total: para 0 has 1 narrator span; para 1 has 1 dialogue span ('"Get ready!"') + 1 narrator span (' he shouted.')
    fake_probs = {
        "emotion": torch.tensor([
            [0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.95] + [0.0] * 7,  # fear (narrator)
            [0.02, 0.0, 0.0, 0.98] + [0.0] * 17,  # anger (dialogue)
            [0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.95] + [0.0] * 7,  # fear (narrator)
        ]),
        "style": torch.tensor([
            [0.99, 0.0, 0.0],   # none
            [0.02, 0.0, 0.98],  # shouting
            [0.99, 0.0, 0.0],   # none
        ]),
        "sfx": torch.tensor([
            [0.99] + [0.0] * 6,
            [0.99] + [0.0] * 6,
            [0.99] + [0.0] * 6,
        ]),
    }
    with patch("tag_novel.load", return_value=(object(), object(), 256)), \
         patch("tag_novel.probs", return_value=fake_probs):
        gw.run_phase1_tagging(bundle, tmp_path / "work", None)

    assert out_tar.exists() and out_novel.exists() and out_ch1.exists()
    with tarfile.open(out_tar, "r:gz") as tf:
        names = tf.getnames()
        assert "shadow-slave_deberta3large_teacher2.jsonl" in names
        assert "chapters/shadow-slave/chapter_1.txt.gz" in names
        assert "SHA256SUMS" in names

    import higgs_format as hf
    ch1_text = gzip.decompress(out_ch1.read_bytes()).decode("utf-8")
    ch1_paras = ch1_text.split("\n\n")
    assert len(ch1_paras) == 2
    assert hf.strip_inline_tags(ch1_paras[0]) == "The cold wind blew."
    assert hf.strip_inline_tags(ch1_paras[1]) == '"Get ready!" he shouted.'
    turns = hf.format_dual(ch1_paras[1], {})
    assert turns == [
        ("dialogue", ['<|emotion:anger|><|style:shouting|>"Get ready!"']),
        ("narrator", ["<|emotion:fear|>he shouted."]),
    ]

