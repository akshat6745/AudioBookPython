"""TTS backends behind one interface: generate(text, seed) -> (float32 mono audio, sample rate, info)."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ORPHEUS = Path(__file__).resolve().parent.parent / "orpheus"
CACHE_LIMIT = 1 << 30


class MlxHiggs:
    """Higgs v3 on Apple silicon via mlx-audio (inside orpheus/sandbox.sh).

    voices: name -> (reference wav, its transcript). Without a reference the speaker changes with the seed.
    """

    def __init__(self, variant: str = "4bit", voices: dict[str, tuple[str, str]] | None = None,
                 sampling: dict | None = None):
        sys.path.insert(0, str(ORPHEUS))
        import higgs_mlx  # needs the sandbox env (ORPHEUS_OUT / ORPHEUS_TEXTS)
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model

        self._mx = mx
        mx.set_cache_limit(CACHE_LIMIT)  # unbounded, MLX's buffer cache grew to 13 GB over a chapter and swapped

        self._synth = higgs_mlx.synth
        if sampling:
            higgs_mlx.SAMPLING = {**higgs_mlx.SAMPLING, **sampling}  # synth() reads it at call time
        self.sampling = higgs_mlx.SAMPLING
        path = higgs_mlx.MODELS / ("higgs_audio_v3" if variant == "bf16" else f"higgs_audio_v3-{variant}")
        self.name = f"higgs_v3-{variant}"
        self.model = load_model(str(path))
        self.ref_ids = {name: hashlib.sha256(Path(wav).read_bytes()).hexdigest()[:16]
                        for name, (wav, _) in (voices or {}).items()}
        self.refs = {name: {"ref_audio_codes": self.model.encode_reference_audio(wav), "ref_text": ref_text}
                     for name, (wav, ref_text) in (voices or {}).items()}
        self._synth(self.model, "Warming up the model.")

    def memory_gb(self) -> float:
        return round(self._mx.get_active_memory() / 1e9, 1)

    def cache_key(self, text: str, seed: int, voice: str | None) -> str:
        """Identifies one generation exactly (model, sampling, reference voice, text, seed)."""
        ident = [self.name, self.sampling, self.ref_ids.get(voice), text, seed]
        return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()

    def generate(self, text: str, seed: int, voice: str | None = None) -> tuple[np.ndarray, int, dict]:
        r = self._synth(self.model, text, seed=seed, **(self.refs[voice] if voice else {}))
        self._mx.clear_cache()
        info = {k: r[k] for k in ("audio_s", "total_s", "runaway", "attempts")}
        return r["audio"], r["sr"], info


def backend(name: str, **kw):
    if name == "mlx":
        return MlxHiggs(**kw)
    if name == "sglang":
        return SglangHiggs(**kw)
    raise SystemExit(f"unknown backend {name!r}")


class SglangHiggs:
    """Higgs v3 on Linux CUDA via SGLang-Omni (127.0.0.1:8000 /v1/audio/speech).

    Registers reference voice clips via /v1/audio/voices once at startup so every chunk keeps a fixed speaker identity.
    """

    def __init__(self, url: str = "http://127.0.0.1:8000", voices: dict[str, tuple[str, str]] | None = None,
                 sampling: dict | None = None, **_):
        sys.path.insert(0, str(ORPHEUS.parent))
        import bench
        import voices as vlib

        self._bench = bench
        self.url = url
        self.name = "higgs_v3-sglang"
        self.sampling = {"temperature": 0.8, "top_k": 50, **(sampling or {})}
        bench.wait_healthy(url, 900)
        self.ref_ids = {}
        self.voice_names = {}
        for role, (wav, ref_text) in (voices or {}).items():
            p = Path(wav)
            digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            vname = f"audiobook_{role}_{digest[:8]}"
            err = vlib.upload_voice(url, vname, p, ref_text, f"{role} reference voice")
            if err and "already" not in err.lower():
                raise SystemExit(f"failed to register voice {role} ({vname}): {err}")
            self.ref_ids[role] = digest
            self.voice_names[role] = vname
        bench.synth_stream(url, "Warming up the model.", self.sampling)

    def memory_gb(self) -> float:
        import shutil
        import subprocess
        if not shutil.which("nvidia-smi"):
            return 0.0
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            ).stdout.split("\n")[0]
            return round(float(out.strip()) / 1024, 1)
        except Exception:
            return 0.0

    def cache_key(self, text: str, seed: int, voice: str | None) -> str:
        ident = [self.name, self.sampling, self.ref_ids.get(voice), text, seed]
        return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()

    def generate(self, text: str, seed: int, voice: str | None = None) -> tuple[np.ndarray, int, dict]:
        import time
        extra = {**self.sampling}
        if voice and voice in self.voice_names:
            extra["voice"] = self.voice_names[voice]
        t0 = time.perf_counter()
        for attempt in range(3):
            req_extra = {**extra, "seed": seed + 1000 * attempt}
            r = self._bench.synth_stream(self.url, text, req_extra)
            if "error" in r:
                raise RuntimeError(f"SGLang TTS error: {r['error']}")
            if not r["runaway"]:
                break
        total_s = time.perf_counter() - t0
        audio = np.frombuffer(r["pcm"], dtype="<i2").astype(np.float32) / 32768.0
        info = {"audio_s": r["audio_s"], "total_s": total_s, "runaway": r["runaway"], "attempts": attempt + 1}
        return audio, r["sr"], info

