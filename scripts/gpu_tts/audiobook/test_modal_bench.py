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


def test_low_utilization_doubles_and_the_middle_band_adds_eight():
    nxt, settled = modal_bench.next_concurrency(2, 30, 4.0, None, 0, 4, 40)
    assert (nxt, settled) == (4, False)
    nxt, settled = modal_bench.next_concurrency(16, 70, 10.0, {"concurrency": 8, "x_realtime": 8.0}, 0, 16, 50)
    assert (nxt, settled) == (24, False)


def test_full_gpu_or_a_slower_step_locks_the_previous_width():
    nxt, settled = modal_bench.next_concurrency(32, 91, 20.0, {"concurrency": 24, "x_realtime": 18.0}, 0, 32, 60)
    assert (nxt, settled) == (32, True)
    nxt, settled = modal_bench.next_concurrency(32, 50, 10.0, {"concurrency": 16, "x_realtime": 18.0}, 0, 32, 60)
    assert (nxt, settled) == (16, True)


def test_errors_and_vram_back_off_and_stop():
    assert modal_bench.next_concurrency(16, 40, 1.0, None, 3, 4, 50) == (8, True)
    assert modal_bench.next_concurrency(16, 40, 8.0, None, 0, 4, 95) == (8, True)
    assert modal_bench.next_concurrency(128, 20, 8.0, None, 0, 4, 50) == (128, True)


def test_ramp_trace_settles_when_the_gpu_is_full():
    ramp = modal_bench.Ramp()
    ramp.observe(8, 2, errors=0, clips=4, sample={"util": 40, "vram_pct": 30})
    assert ramp.workers == 4 and not ramp.settled
    ramp.observe(20, 2, errors=0, clips=8, sample={"util": 93, "vram_pct": 70})
    assert ramp.workers == 4 and ramp.settled
    assert [row["concurrency"] for row in ramp.trace] == [2, 4]


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


def test_queue_command_renders_the_whole_novel_on_l4():
    cmd = queue_modal_task.command({"gpu": "L4", "chapters": "", "novels": ["the-shining"]})
    assert cmd == ["modal", "run", "--detach", "modal_app.py::render_l4", "--novel=the-shining"]


def test_request_file_stays_disarmed():
    request = json.loads(Path(__file__).parents[3].joinpath("render_request.json").read_text())
    assert request["run"] is False
    assert request["gpu"] == "L4"
    assert request["chapters"] == ""
    assert request["novels"] == ["the-shining"]
