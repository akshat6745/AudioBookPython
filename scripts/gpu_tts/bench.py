#!/usr/bin/env python3
"""Benchmark + listen client for OpenAI-style /v1/audio/speech TTS servers (SGLang-Omni).

Stdlib only, so it runs unchanged on the GPU box and on the Mac.

  bench:  python3 bench.py bench --url http://localhost:8000 --name higgs_v3
  listen: python3 bench.py play  --url http://localhost:8000 --text texts/passage.txt
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

# The server is only ever reached on the box itself or through an SSH tunnel.
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}

HERE = Path(__file__).resolve().parent
TEXTS = HERE / "texts"
DEFAULT_SR = 24000
PARAGRAPH_GAP_S = 0.4
CHARS_PER_S = 15  # measured Higgs v3 speaking rate; > RUNAWAY_X times longer means it didn't stop
RUNAWAY_X = 2.5
CHUNK_CHARS = 500  # ~30-40 s of audio; well under Higgs' 2048-frame (~82 s) cap


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ALLOWED_HOSTS:
        sys.exit(f"--url must be http(s)://localhost:<port> (use an SSH tunnel), got {url}")


def wait_healthy(url: str, timeout_s: float) -> None:
    check_url(url)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    sys.exit(f"server at {url} not healthy after {timeout_s:.0f}s")


def synth_stream(url: str, text: str, extra: dict, on_chunk=None) -> dict:
    """Stream 16-bit mono PCM; return timings + audio bytes."""
    check_url(url)
    body = {"input": text, "stream": True, "response_format": "pcm", **extra}
    req = urllib.request.Request(
        f"{url}/v1/audio/speech",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    ttfa = None
    chunks: list[bytes] = []
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            sr = int(r.headers.get("x-sample-rate") or DEFAULT_SR)
            while True:
                chunk = r.read1(65536)
                if not chunk:
                    break
                if ttfa is None:
                    ttfa = time.perf_counter() - t0
                chunks.append(chunk)
                if on_chunk:
                    on_chunk(chunk)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"}
    except (urllib.error.URLError, OSError) as e:
        return {"error": str(e)}
    total = time.perf_counter() - t0
    pcm = b"".join(chunks)
    audio_s = len(pcm) / 2 / sr
    return {
        "pcm": pcm,
        "sr": sr,
        "ttfa_s": ttfa,
        "total_s": total,
        "audio_s": audio_s,
        "rtf": total / audio_s if audio_s else None,
        "runaway": audio_s > RUNAWAY_X * max(1.0, len(text) / CHARS_PER_S),
    }


def write_wav(path: Path, pcm: bytes, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


def chunk_paragraphs(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Merge paragraphs into ~max_chars chunks without splitting a paragraph."""
    chunks, cur = [], ""
    for p in (p.strip() for p in text.split("\n\n")):
        if not p:
            continue
        if cur and len(cur) + len(p) + 1 > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


class GpuSampler:
    """Polls nvidia-smi for peak VRAM / utilisation; no-op without a GPU."""

    def __init__(self) -> None:
        self.available = shutil.which("nvidia-smi") is not None
        self.peak_mem_mib = 0
        self.peak_util = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.split("\n")[0]
                mem, util = (int(x.strip()) for x in out.split(","))
                self.peak_mem_mib = max(self.peak_mem_mib, mem)
                self.peak_util = max(self.peak_util, util)
            except (subprocess.SubprocessError, ValueError):
                pass
            self._stop.wait(0.5)

    def __enter__(self):
        if self.available:
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()


def fmt(v, nd=2) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


def texts_for(name: str) -> dict[str, str]:
    """passage.txt plus a model-specific tagged variant (passage.<name>.txt) if present."""
    out = {"passage": (TEXTS / "passage.txt").read_text().strip()}
    tagged = TEXTS / f"passage.{name}.txt"
    if tagged.exists():
        out["passage_tagged"] = tagged.read_text().strip()
    return out


def cmd_bench(a: argparse.Namespace) -> None:
    out_dir = Path(a.out or HERE / "outputs" / a.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    extra = json.loads(a.extra)
    rows: list[dict] = []

    log(f"waiting for {a.url}/health")
    wait_healthy(a.url, a.health_timeout)
    log("warm-up request")
    synth_stream(a.url, "Hello, this is a warm-up.", extra)

    with GpuSampler() as gpu:
        # 1. Quality samples: short passages, one request each.
        for label, text in texts_for(a.name).items():
            r = synth_stream(a.url, text, extra)
            if "error" in r:
                log(f"{label}: ERROR {r['error']}")
                rows.append({"test": label, "error": r["error"]})
                continue
            write_wav(out_dir / f"{label}.wav", r["pcm"], r["sr"])
            log(f"{label}: ttfa={fmt(r['ttfa_s'])}s audio={fmt(r['audio_s'],1)}s rtf={fmt(r['rtf'])}{' RUNAWAY' if r['runaway'] else ''}")
            rows.append({"test": label, "concurrency": 1, **{k: r[k] for k in ("ttfa_s", "total_s", "audio_s", "rtf", "runaway")}})

        # 2. Real chapter: paragraph chunks in order, stitched into one listenable file.
        if not a.skip_chapter:
            chunks = chunk_paragraphs((TEXTS / "chapter_excerpt.txt").read_text())
            pcm_all, sr, ttfas, t_total, a_total, runaways = [], DEFAULT_SR, [], 0.0, 0.0, 0
            for i, chunk in enumerate(chunks, 1):
                r = synth_stream(a.url, chunk, extra)
                if "error" in r:
                    log(f"chapter chunk {i}/{len(chunks)}: ERROR {r['error']}")
                    continue
                sr = r["sr"]
                pcm_all += [r["pcm"], b"\x00\x00" * int(sr * PARAGRAPH_GAP_S)]
                ttfas.append(r["ttfa_s"])
                t_total += r["total_s"]
                a_total += r["audio_s"]
                runaways += r["runaway"]
                log(f"chapter chunk {i}/{len(chunks)}: ttfa={fmt(r['ttfa_s'])}s rtf={fmt(r['rtf'])}{' RUNAWAY' if r['runaway'] else ''}")
            if pcm_all:
                write_wav(out_dir / "chapter_excerpt.wav", b"".join(pcm_all), sr)
                rows.append({
                    "test": f"chapter_excerpt ({len(chunks)} chunks)", "concurrency": 1,
                    "ttfa_s": sum(ttfas) / len(ttfas), "total_s": t_total, "audio_s": a_total,
                    "rtf": t_total / a_total if a_total else None, "runaway": runaways,
                })

        # 3. Concurrency sweep: N simultaneous listeners on the plain passage.
        passage = (TEXTS / "passage.txt").read_text().strip()
        for n in (int(x) for x in a.concurrency.split(",")):
            t0 = time.perf_counter()
            with ThreadPoolExecutor(n) as pool:
                results = list(pool.map(lambda _: synth_stream(a.url, passage, extra), range(n)))
            wall = time.perf_counter() - t0
            ok = [r for r in results if "error" not in r]
            if not ok:
                log(f"concurrency {n}: all failed: {results[0]['error']}")
                rows.append({"test": "concurrency", "concurrency": n, "error": results[0]["error"]})
                continue
            row = {
                "test": "concurrency", "concurrency": n,
                "ttfa_s": sum(r["ttfa_s"] for r in ok) / len(ok),
                "total_s": sum(r["total_s"] for r in ok) / len(ok),
                "audio_s": sum(r["audio_s"] for r in ok) / len(ok),
                "rtf": max(r["rtf"] for r in ok),  # worst listener decides stutter
                "audio_s_per_s": sum(r["audio_s"] for r in ok) / wall,
                "failed": n - len(ok),
                "runaway": sum(r["runaway"] for r in ok),
            }
            log(f"concurrency {n}: ttfa={fmt(row['ttfa_s'])}s worst_rtf={fmt(row['rtf'])} audio_s/s={fmt(row['audio_s_per_s'],1)}")
            rows.append(row)

    fields = ["test", "concurrency", "ttfa_s", "total_s", "audio_s", "rtf", "audio_s_per_s", "runaway", "failed", "error"]
    with open(out_dir / "results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    summary = {"name": a.name, "url": a.url, "extra": extra, "rows": rows,
               "peak_vram_mib": gpu.peak_mem_mib if gpu.available else None,
               "peak_gpu_util": gpu.peak_util if gpu.available else None}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    print(f"\n== {a.name} ==  (rtf < 1 = faster than realtime)")
    print(f"{'test':34} {'conc':>4} {'ttfa_s':>7} {'audio_s':>8} {'rtf':>6} {'aud_s/s':>8} {'runaway':>7}")
    for r in rows:
        if r.get("error"):
            print(f"{r['test']:34} {r.get('concurrency', ''):>4}  ERROR {r['error'][:60]}")
            continue
        print(f"{r['test']:34} {r['concurrency']:>4} {fmt(r['ttfa_s']):>7} {fmt(r['audio_s'],1):>8} "
              f"{fmt(r['rtf']):>6} {fmt(r.get('audio_s_per_s'),1):>8} {int(r.get('runaway') or 0):>7}")
    if gpu.available:
        print(f"peak VRAM {gpu.peak_mem_mib} MiB, peak GPU util {gpu.peak_util}%")
    log(f"wrote {out_dir}")


def cmd_play(a: argparse.Namespace) -> None:
    """Play audio while it streams (sox `play`, else ffplay) and report real first-audio latency."""
    text = Path(a.text).read_text().strip() if Path(a.text).exists() else a.text
    extra = json.loads(a.extra)
    player = None
    if shutil.which("play"):
        cmd = ["play", "-q", "-t", "raw", "-r", str(DEFAULT_SR), "-e", "signed", "-b", "16", "-c", "1", "-"]
    elif shutil.which("ffplay"):
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-f", "s16le", "-ar", str(DEFAULT_SR), "-ac", "1", "-"]
    else:
        cmd = None
    if cmd and not a.no_audio:
        player = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def feed(chunk: bytes) -> None:
        if player and player.stdin:
            player.stdin.write(chunk)
            player.stdin.flush()

    log(f"requesting {len(text)} chars from {a.url}")
    r = synth_stream(a.url, text, extra, on_chunk=feed)
    if player and player.stdin:
        player.stdin.close()
        player.wait()
    if "error" in r:
        sys.exit(f"ERROR {r['error']}")
    log(f"first audio after {fmt(r['ttfa_s'])}s | {fmt(r['audio_s'],1)}s audio in {fmt(r['total_s'])}s | rtf {fmt(r['rtf'])}")
    if a.save:
        write_wav(Path(a.save), r["pcm"], r["sr"])
        log(f"saved {a.save}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default="http://localhost:8000")
    common.add_argument("--extra", default='{"temperature": 0.8, "top_k": 50}',
                        help="JSON merged into every request (sampling params, voice, references)")

    b = sub.add_parser("bench", parents=[common])
    b.add_argument("--name", required=True, help="model label, e.g. higgs_v3 (also picks passage.<name>.txt)")
    b.add_argument("--out", help="output dir (default outputs/<name>)")
    b.add_argument("--concurrency", default="1,2,4")
    b.add_argument("--skip-chapter", action="store_true")
    b.add_argument("--health-timeout", type=float, default=900)
    b.set_defaults(func=cmd_bench)

    pl = sub.add_parser("play", parents=[common])
    pl.add_argument("--text", default=str(TEXTS / "passage.txt"), help="file path or literal text")
    pl.add_argument("--save", help="also save the streamed audio as WAV")
    pl.add_argument("--no-audio", action="store_true", help="measure only, don't play")
    pl.set_defaults(func=cmd_play)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
