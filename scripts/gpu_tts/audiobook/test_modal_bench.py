"""Adaptive concurrency decisions and the pending-QA manifest, without a GPU or Modal."""
import hashlib
import json
from pathlib import Path

import numpy as np

import higgs_format as hf
import modal_bench
import monitor_modal_render
import queue_modal_task
import render
import stop_modal_render

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


def test_stop_targets_only_the_live_audiobook_app():
    apps = [
        {"description": "audiobook-render", "state": "ephemeral (detached)", "app_id": "ap-live"},
        {"description": "audiobook-render", "state": "stopped", "app_id": "ap-old"},
        {"description": "other-app", "state": "ephemeral", "app_id": "ap-other"},
    ]
    named = stop_modal_render.audiobook_apps(apps)
    assert [app["app_id"] for app in named] == ["ap-live", "ap-old"]
    active = [app for app in named if app["state"] in stop_modal_render.ACTIVE]
    assert [app["app_id"] for app in active] == ["ap-live"]


def test_queue_command_renders_multiple_novels_on_l40s():
    cmd = queue_modal_task.command({"gpu": "L40S", "chapters": "", "novels": ["the-shining", "the-eye-of-the-world"]})
    assert cmd == [
        "modal", "run", "--detach", "modal_app.py::queue_l40s",
        "--novels=the-shining,the-eye-of-the-world",
    ]


def test_monitor_requires_health_before_real_render():
    healthy, rendered = monitor_modal_render.observe(
        "[phase 2] the-shining ch6: rendered 4.2m audio in 10.0s", False
    )
    assert not healthy and not rendered
    healthy, rendered = monitor_modal_render.observe(
        "healthy at concurrency 64 after 120s", healthy
    )
    assert healthy and not rendered
    healthy, rendered = monitor_modal_render.observe(
        "[phase 2] the-shining ch6: rendered 4.2m audio in 10.0s", healthy
    )
    assert healthy and rendered


def test_server_uses_image_v010_indexed_configuration():
    root = Path(__file__).parents[3]
    serve = root.joinpath("scripts/gpu_tts/higgs_v3/serve.sh").read_text()
    setup = root.joinpath("scripts/gpu_tts/higgs_v3/setup.sh").read_text()
    assert "--stages.0.factory_args.max_concurrency" in serve
    assert "--stages.1.factory_args.max_batch_size" in serve
    assert (
        "--stages.2.factory_args.server_args_overrides.max_running_requests" in serve
    )
    assert "--stages.2.factory_args.server_args_overrides.cuda_graph_max_bs" in serve
    assert "--stages.3.factory_args.max_batch_size" in serve
    assert "validate_config.py" in setup
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
