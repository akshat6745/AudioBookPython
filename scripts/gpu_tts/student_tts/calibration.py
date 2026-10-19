"""Pure GPU calibration policy; safe to unit-test without CUDA."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Measurement:
    setting: int
    stable: bool
    throughput: float
    vram_used_gb: float
    vram_total_gb: float
    utilization_pct: float
    step_s: float = 0.0
    error: str = ""

    @property
    def headroom_pct(self) -> float:
        return 100 * (self.vram_total_gb - self.vram_used_gb) / max(self.vram_total_gb, 0.001)


def choose_higgs(measurements: Iterable[Measurement], default: int = 64) -> Measurement:
    """Prefer passed audio/second; 72/80 need stability, headroom and a real gain."""
    rows = {m.setting: m for m in measurements}
    base = rows.get(default)
    if not base or not base.stable:
        raise ValueError("the proven 64-stream Higgs baseline must pass")
    best = base
    for setting in (72, 80):  # never probe 128
        m = rows.get(setting)
        if m and m.stable and m.headroom_pct >= 5 and m.throughput >= best.throughput * 1.03:
            best = m
    return best


def choose_training(measurements: Iterable[Measurement], min_headroom_pct: float = 7.5) -> Measurement:
    good = [m for m in measurements if m.stable and m.headroom_pct >= min_headroom_pct]
    if not good:
        raise ValueError("no stable physical batch with required VRAM headroom")
    return max(good, key=lambda m: (m.throughput, m.setting))


def next_smaller_batch(failed: int, candidates: tuple[int, ...] = (8, 12, 16, 24)) -> int:
    lower = [b for b in candidates if b < failed]
    if not lower:
        raise RuntimeError(f"OOM at minimum physical batch {failed}")
    return max(lower)


def save_choice(path: Path, gpu_name: str, kind: str, chosen: Measurement, trace: Iterable[Measurement],
                extra: dict | None = None) -> None:
    """Persist only non-secret telemetry with owner-only permissions."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(path.read_text()) if path.exists() else {}
    old.setdefault(gpu_name, {})[kind] = {
        "chosen": asdict(chosen), "trace": [asdict(m) for m in trace], **(extra or {})
    }
    path.write_text(json.dumps(old, indent=2, sort_keys=True))
    os.chmod(path, 0o600)


def load_choice(path: Path, gpu_name: str, kind: str) -> dict | None:
    path = path.resolve()
    return json.loads(path.read_text()).get(gpu_name, {}).get(kind) if path.exists() else None
