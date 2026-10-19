"""Low-overhead NVIDIA telemetry emitted as JSON lines."""
from __future__ import annotations

import json
import statistics
import subprocess
import threading
import time


def gpu_sample() -> dict | None:
    try:
        raw = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=timestamp,name,utilization.gpu,memory.used,memory.total,power.draw,power.limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.splitlines()[0]
        timestamp, name, util, used, total, power, power_limit = [x.strip() for x in raw.split(",")]
        return {
            "timestamp": timestamp, "gpu": name, "utilization_pct": float(util),
            "vram_used_mb": float(used), "vram_total_mb": float(total),
            "power_w": float(power), "power_limit_w": float(power_limit),
        }
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


class Monitor:
    def __init__(self, queue_depth=lambda: 0, interval_s: float = 5):
        self.queue_depth, self.interval_s = queue_depth, interval_s
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                sample = gpu_sample()
                if sample:
                    sample["queue_depth"] = int(self.queue_depth())
                    self.samples.append(sample)
                    print("GPU_TELEMETRY " + json.dumps(sample), flush=True)
                self._stop.wait(self.interval_s)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s + 1)

    def summary(self) -> dict:
        if not self.samples:
            return {}
        return {
            "median_utilization_pct": round(statistics.median(s["utilization_pct"] for s in self.samples), 1),
            "peak_vram_mb": max(s["vram_used_mb"] for s in self.samples),
            "median_power_w": round(statistics.median(s["power_w"] for s in self.samples), 1),
        }
