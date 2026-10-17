"""cloud_render.build_job against an in-memory R2/D1, render.render's parallel mode, and Opus encoding."""
import hashlib
import io
import json
import shutil
import subprocess
import wave

import numpy as np
import pytest

import cloud_render as cr
import gpu_library_worker as gw
import higgs_format as hf
import render

SR = 24000


class FakeR2:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://r2.test/{op}/{Params['Key']}"

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def get_paginator(self, _):
        objects = self.objects

        class Pages:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}]

        return Pages()


PREFIX = "audio/book/higgs_v3-default_seed8-default_seed2"


def tags(ch: int, n: int) -> str:
    return "".join(json.dumps({"chapter": ch, "paragraph": i, "emotion": "neutral", "style": "none", "sfx": "none",
                               "confidence": 0.9, "style_conf": 0.9, "sfx_conf": 0.9}) + "\n" for i in range(n))


@pytest.fixture
def library():
    texts = {1: "One.\n\nTwo.", 2: "Only one.", 3: "A.\n\nB.\n\nC.", 4: "Done already.", 5: "Held before."}
    objects = {f"chapters/book/{ch}.txt": t.encode() for ch, t in texts.items()}
    objects[f"{cr.ML_PREFIX}/tags/book_{cr.TAG_MODEL}.jsonl"] = (
        tags(1, 2) + tags(2, 1) + tags(3, 2) + tags(4, 1) + tags(5, 1)).encode()  # ch3 has 3 paragraphs, 2 tags
    objects[f"{PREFIX}/chapter_4/manifest.json"] = b"{}"
    objects[f"{PREFIX}/chapter_5/held.json"] = b"{}"
    rows = [{"chapter_number": ch, "title": f"T{ch}", "r2_content_path": f"chapters/book/{ch}.txt"} for ch in texts]
    return FakeR2(objects), lambda sql, params: rows if params == ["book"] else []


def test_build_job_queues_only_new_chapters_with_matching_tags(tmp_path, library):
    s3, query = library
    bundle, report = cr.build_job(s3, "b", query, tmp_path, ["book"])
    assert report["book"] == {"chapters": 5, "done": 1, "held": [5], "tag_mismatch": [3], "queued": 2}
    [job] = bundle["novels"]
    assert [c["chapter"] for c in job["chapters"]] == [1, 2]
    ch1 = job["chapters"][0]
    assert sorted(ch1["put_mp3_urls"]) == ["0.opus", "1.opus", "title.opus"]
    assert ch1["put_held_url"].endswith(f"{PREFIX}/chapter_1/held.json")
    assert bundle["settings"]["format"] == "opus" and bundle["settings"]["workers"] == 8
    assert (tmp_path / "novels/book/chapters/00001.txt").read_text() == "One.\n\nTwo."
    lines = (tmp_path / "novels/book/tags/00001.jsonl").read_text().splitlines()
    assert [json.loads(ln)["paragraph"] for ln in lines] == [0, 1] and "chapter" not in json.loads(lines[0])


def test_retry_held_requeues_held_chapters(tmp_path, library):
    s3, query = library
    bundle, report = cr.build_job(s3, "b", query, tmp_path, ["book"], chapters={4, 5}, retry_held=True)
    assert [c["chapter"] for c in bundle["novels"][0]["chapters"]] == [5]


def test_missing_tags_stop_the_run(tmp_path, library):
    s3, query = library
    with pytest.raises(RuntimeError, match="no ensemble tags"):
        cr.build_job(s3, "b", query, tmp_path, ["other"])


class SeededTTS:
    """Audio depends only on (text, seed), like the real model with a fixed seed."""

    name = "seeded"

    def cache_key(self, text, seed, voice):
        return hashlib.sha256(json.dumps([text, seed, voice]).encode()).hexdigest()

    def generate(self, text, seed, voice):
        rng = np.random.default_rng(seed)
        audio = (0.3 * np.sin(np.linspace(0, 900, SR + 100 * len(text))) * rng.uniform(0.5, 1)).astype(np.float32)
        return audio, SR, {"audio_s": len(audio) / SR, "total_s": 0.01, "runaway": False, "attempts": 1}

    def memory_gb(self):
        return 0.0


def test_parallel_render_matches_sequential(tmp_path):
    ps = [f"Paragraph number {i} says something." for i in range(12)]
    ts = [{"emotion": "neutral", "style": "none", "sfx": "none", "confidence": 1, "style_conf": 1, "sfx_conf": 1}] * 12
    reqs = render.plan(ps, ts, "dual", hf.Cuts())
    outs = {}
    for workers in (1, 4):
        out = tmp_path / str(workers)
        render.render(SeededTTS(), "n", 1, "dual", "Chapter 1: T", ps, reqs, ts, out, lambda m: None,
                      workers=workers, ext=".opus")
        outs[workers] = out
    assert (outs[1] / "wav/dual/00001.wav").read_bytes() == (outs[4] / "wav/dual/00001.wav").read_bytes()
    man = json.loads((outs[4] / "manifest/dual/00001.json").read_text())
    assert man["title"]["file"] == "title.opus" and [e["file"] for e in man["paragraphs"]][:2] == ["0.opus", "1.opus"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_opus_encoding(tmp_path):
    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes((np.sin(np.linspace(0, 3000, SR * 2)) * 8000).astype("<i2").tobytes())
    out = tmp_path / "a.opus"
    gw.to_mp3(wav, out, "volume=0dB", "64k", "opus")
    assert out.read_bytes()[:4] == b"OggS"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True)
    assert probe.stdout.strip() == "opus"
