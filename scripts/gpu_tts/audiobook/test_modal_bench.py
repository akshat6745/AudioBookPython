"""Concurrency sweep decisions, without a GPU or Modal."""
import json
from pathlib import Path

import higgs_format as hf
import modal_bench
import queue_modal_task


def test_sweep_stops_when_more_requests_get_slower():
    clips = [("narrator", "Hello.")] * 4
    clock = iter([0, 10, 10, 20, 20, 40])

    def now():
        return next(clock)

    state = {"i": 0}

    def generate(voice, text):
        audio = [2.5, 7.5, 5.0][state["i"] // 4]
        state["i"] += 1
        return audio

    report = modal_bench.sweep(clips, [8, 16, 32], generate, now)
    assert [row["concurrency"] for row in report["levels"]] == [8, 16, 32]
    assert [row["x_realtime"] for row in report["levels"]] == [1.0, 3.0, 1.0]
    assert report["best_concurrency"] == 16


def test_sweep_stops_when_half_the_clips_fail():
    def generate(voice, text):
        raise RuntimeError("oom")

    report = modal_bench.sweep([("narrator", "Hello.")] * 4, [8, 16], generate)
    assert [row["concurrency"] for row in report["levels"]] == [8]
    assert report["levels"][0]["errors"] == 4


def test_collect_clips_keeps_voice_and_caps_the_set():
    tag = {"emotion": "neutral", "style": "none", "sfx": "none", "confidence": 1, "style_conf": 1, "sfx_conf": 1}
    texts = {1: ["Alpha said hello.", "Beta walked on."], 2: ["Gamma asked why."]}
    tags = {1: [tag, tag], 2: [tag]}
    clips = modal_bench.collect_clips(texts, tags, limit=2)
    assert len(clips) == 2
    assert clips[0][0] in {"narrator", "dialogue"}
    assert "Alpha" in hf.plain(clips[0][1])


def test_queue_command_selects_the_gpu_function():
    cmd = queue_modal_task.command({"gpu": "L4", "chapters": "1-3", "levels": [8, 16], "novels": ["the-shining"]})
    assert cmd == ["modal", "run", "--detach", "modal_app.py::benchmark_l4",
                   "--chapters=1-3", "--levels=8,16", "--novel=the-shining"]


def test_request_file_stays_disarmed():
    request = json.loads(Path(__file__).parents[3].joinpath("render_request.json").read_text())
    assert request["run"] is False
    assert request["gpu"] == "L40S"
