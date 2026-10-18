"""Adaptive concurrency decisions and the pending-QA manifest, without a GPU or Modal."""
import hashlib
import json
from pathlib import Path

import numpy as np

import higgs_format as hf
import modal_bench
import queue_modal_task
import render

SR = 24000


def test_requested_width_settles_despite_misleading_gpu_metrics():
    nxt, settled = modal_bench.next_concurrency(128, 99, 20.0, None, 0, 256, 98)
    assert (nxt, settled) == (128, True)


def test_errors_back_off_but_reserved_vram_does_not():
    assert modal_bench.next_concurrency(128, 40, 0.0, None, 4, 4, 50) == (64, False)
    assert modal_bench.next_concurrency(64, 99, 8.0, None, 0, 128, 98) == (64, True)


def test_ramp_starts_at_128_and_backs_off_until_success():
    ramp = modal_bench.Ramp()
    assert ramp.workers == 128
    ramp.observe(0, 1, errors=256, clips=256, sample={"util": 99, "vram_pct": 98})
    assert ramp.workers == 64 and not ramp.settled
    ramp.observe(20, 2, errors=0, clips=128, sample={"util": 99, "vram_pct": 98})
    assert ramp.workers == 64 and ramp.settled
    assert [row["concurrency"] for row in ramp.trace] == [128, 64]


class QuietTTS:
    name = "quiet"

    def cache_key(self, text, seed, voice):
        return hashlib.sha256(f"{voice}:{text}:{seed}".encode()).hexdigest()

    def generate(self, text, seed, voice=None):
        audio = np.zeros(SR // 2, np.float32)
        audio[0] = 0.2
        return audio, SR, {"audio_s": 0.5, "total_s": 0.01, "runaway": False, "attempts": 1}

    def memory_gb(self):
        return 0.0


def test_manifest_is_marked_pending_without_a_whisper_check(tmp_path):
    tag = {"emotion": "neutral", "style": "none", "sfx": "none", "confidence": 1, "style_conf": 1, "sfx_conf": 1}
    ps = ["Alpha said hello.", "Beta walked on."]
    reqs = render.plan(ps, [tag, tag], "dual", hf.Cuts())
    ramp = modal_bench.Ramp()
    ramp.settled = True
    ramp.workers = 2

    render.render(QuietTTS(), "the-shining", 1, "dual", "Chapter 1: T", ps, reqs, [tag, tag], tmp_path,
                  lambda _m: None, check=None, ramp=ramp, qa_status="pending-whisper", ext=".opus")
    manifest = json.loads((tmp_path / "manifest/dual/00001.json").read_text())
    assert manifest["qa"] == "pending-whisper"
    assert manifest["title"]["file"] == "title.opus"


def test_queue_command_renders_multiple_novels_on_l40s():
    cmd = queue_modal_task.command({"gpu": "L40S", "chapters": "", "novels": ["the-shining", "the-eye-of-the-world"]})
    assert cmd == [
        "modal", "run", "--detach", "modal_app.py::queue_l40s",
        "--novels=the-shining,the-eye-of-the-world",
    ]


def test_server_uses_stage_scoped_engine_configuration():
    root = Path(__file__).parents[3]
    serve = root.joinpath("scripts/gpu_tts/higgs_v3/serve.sh").read_text()
    setup = root.joinpath("scripts/gpu_tts/higgs_v3/setup.sh").read_text()
    assert "--tts_engine.engine.max_running_requests" in serve
    assert "--tts_engine.engine.cuda_graph_max_bs" in serve
    assert 'pip install --python "$PY"' not in setup


def test_request_file_has_valid_launch_schema():
    request = json.loads(Path(__file__).parents[3].joinpath("render_request.json").read_text())
    assert isinstance(request["run"], bool)
    assert request["gpu"] == "L40S"
    assert request["chapters"] == ""
    assert request["novels"] == [
        "the-shining",
        "haunting-adeline-cat-and-mouse-duet-book-1",
        "the-eye-of-the-world",
        "oathbringer-the-stormlight-archive-book-3",
        "lord-of-the-mysteries",
    ]
