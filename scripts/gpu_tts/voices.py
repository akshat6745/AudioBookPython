#!/usr/bin/env python3
"""Voice library + emotion showcase for Higgs v3 (zero-shot voice cloning).

Higgs v3 has one built-in voice ("default"); other voices come from cloning a short
reference clip. References here are edge-tts voices — fine for internal evaluation only;
production voices need rights (e.g. public-domain LibriVox narrators, consenting actors).

  python3 voices.py refs                      # Mac: render reference clips with edge-tts
  python3 voices.py upload   --url http://localhost:8000
  python3 voices.py showcase --url http://localhost:8000   # every voice reads the passage
  python3 voices.py emotions --url http://localhost:8000 [--voices default,andrew,sonia,prabhat]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bench  # check_url, synth_stream, write_wav, log

HERE = Path(__file__).resolve().parent
REFS = HERE / "texts" / "voices"
OUT = HERE / "outputs"
REF_TEXT = (
    "The old lighthouse stood at the edge of the world. Every night, its keeper climbed "
    "the winding stairs, lit the great lamp, and watched the dark sea for ships that never came."
)
MAX_ATTEMPTS = 3
SHOWCASE_TEXT = (HERE / "texts" / "passage.txt").read_text().strip()

# Sentence-level tags go first; sfx tags are followed directly by their onomatopoeia (see PROMPTING.md).
EMOTIONS = {
    "neutral": "The caravan moved slowly through the snow, and the night was quiet.",
    "anger": "<|emotion:anger|>How dare you lie to me! After everything I did for you, this is how you repay me?",
    "anger_shouting": "<|emotion:anger|><|style:shouting|>Get out! Get out of my house, right now!",
    "excited": "<|emotion:enthusiasm|><|prosody:speed_fast|>We did it! We actually did it! I can't believe we finally made it to the top!",
    "joy": "<|emotion:elation|>She said yes! Oh, this is the best day of my entire life!",
    "sad": "<|emotion:sadness|><|prosody:speed_slow|>He never came home that night. I waited by the window until the candles burned out.",
    "crying": "<|emotion:sadness|><|sfx:crying|>Sob, I'm sorry... I'm so sorry. I should have been there.",
    "fear": "<|emotion:fear|>Did you hear that? Something is moving in the dark. We need to leave, now.",
    "whisper": "<|style:whispering|>Stay close to me, and don't make a sound. They're right outside the door.",
    "surprise": "<|emotion:surprise|><|prosody:pitch_high|>Wait, you're alive? But I saw you fall from the cliff!",
    "amused_laugh": "<|emotion:amusement|><|sfx:laughter|>Haha, you should have seen his face when the chicken stole his hat!",
    "contemplative": "<|emotion:contemplation|><|prosody:speed_slow|>Perhaps, in the end, every road leads back to where we started.",
    "determined": "<|emotion:determination|>I will not run. Not today. If they want this city, they will have to take it from me.",
    "tender": "<|emotion:affection|>Come here, little one. You're safe now. I've got you.",
    "bitter": "<|emotion:bitterness|>Of course they chose him. They always choose him.",
    "sigh_helpless": "<|emotion:helplessness|><|sfx:sigh|>Haah, there's nothing more we can do.",
}


def voice_name(short_name: str) -> str:
    """en-GB-RyanNeural -> gb_ryan ; en-IN-NeerjaExpressiveNeural -> in_neerjaexpressive."""
    _, region, rest = short_name.split("-", 2)
    rest = rest.replace("MultilingualNeural", "").replace("Neural", "")
    return f"{region.lower()}_{rest.lower()}"


def cmd_refs(_: argparse.Namespace) -> None:
    import edge_tts

    voices = asyncio.run(edge_tts.list_voices())
    english = sorted((v for v in voices if v["Locale"].startswith("en-")), key=lambda v: v["ShortName"])
    # Drop "Multilingual" duplicates of the same speaker.
    names = {v["ShortName"] for v in english}
    english = [v for v in english if not ("Multilingual" in v["ShortName"]
                                          and v["ShortName"].replace("Multilingual", "") in names)]
    REFS.mkdir(parents=True, exist_ok=True)
    manifest = []

    async def render(v: dict) -> None:
        name = voice_name(v["ShortName"])
        path = REFS / f"{name}.mp3"
        if not path.exists():
            await edge_tts.Communicate(REF_TEXT, v["ShortName"]).save(str(path))
        desc = f"{v['Gender'].lower()}, {v['LocaleName']}, " + ", ".join(v.get("VoiceTag", {}).get("VoicePersonalities", []))
        manifest.append({"name": name, "source": v["ShortName"], "description": desc, "file": path.name})

    async def run_all() -> None:
        sem = asyncio.Semaphore(6)

        async def one(v):
            async with sem:
                await render(v)

        await asyncio.gather(*(one(v) for v in english))

    asyncio.run(run_all())
    manifest.sort(key=lambda m: m["name"])
    (REFS / "manifest.json").write_text(json.dumps({"ref_text": REF_TEXT, "voices": manifest}, indent=2))
    bench.log(f"{len(manifest)} reference clips in {REFS}")


def upload_voice(url: str, name: str, audio: Path, ref_text: str, description: str) -> str | None:
    """Multipart POST to /v1/audio/voices. Returns an error string, or None on success."""
    bench.check_url(url)
    boundary = uuid.uuid4().hex
    fields = {
        "name": name,
        "consent": "internal-eval-synthetic-edge-tts-reference",
        "ref_text": ref_text,
        "speaker_description": description,
    }
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()
    )
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="audio_sample"; filename="{audio.name}"\r\n'
             f"Content-Type: audio/mpeg\r\n\r\n").encode() + audio.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{url}/v1/audio/voices", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60):
            return None
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"
    except (urllib.error.URLError, OSError) as e:
        return str(e)


def load_manifest() -> dict:
    path = REFS / "manifest.json"
    if not path.exists():
        sys.exit("no texts/voices/manifest.json — run `voices.py refs` first")
    return json.loads(path.read_text())


def cmd_upload(a: argparse.Namespace) -> None:
    m = load_manifest()
    for v in m["voices"]:
        err = upload_voice(a.url, v["name"], REFS / v["file"], m["ref_text"], v["description"])
        bench.log(f"upload {v['name']:22} {'ok' if not err else 'ERROR ' + err}")


def render_jobs(a: argparse.Namespace, jobs: list[tuple[str, str, dict, Path]]) -> None:
    """jobs: (label, text, extra, out_path). Parallel to hide tunnel latency; reports runaways."""
    base = json.loads(a.extra)

    def run(job):
        label, text, extra, path = job
        for attempt in range(MAX_ATTEMPTS):
            # ~5% of generations never emit end-of-speech; a fresh seed almost always fixes it.
            seed = {"seed": extra.get("seed", 0) + 1000 * attempt} if attempt else {}
            r = bench.synth_stream(a.url, text, {**base, **extra, **seed})
            if "error" in r or not r["runaway"]:
                break
        if "error" in r:
            return f"{label:40} ERROR {r['error'][:80]}"
        bench.write_wav(path, r["pcm"], r["sr"])
        flag = " RUNAWAY" if r["runaway"] else (f" (retried x{attempt})" if attempt else "")
        return f"{label:40} {r['audio_s']:5.1f}s audio  ttfa {r['ttfa_s']:.2f}s{flag}"

    with ThreadPoolExecutor(a.parallel) as pool:
        for line in pool.map(run, jobs):
            bench.log(line)


def cmd_showcase(a: argparse.Namespace) -> None:
    out = OUT / "voices"
    jobs = [(f"default seed {s}", SHOWCASE_TEXT, {"seed": s}, out / f"default_seed{s}.wav") for s in range(1, 5)]
    jobs += [(v["name"], SHOWCASE_TEXT, {"voice": v["name"]}, out / f"{v['name']}.wav") for v in load_manifest()["voices"]]
    render_jobs(a, jobs)
    bench.log(f"wrote {out}")


def cmd_emotions(a: argparse.Namespace) -> None:
    out = OUT / "emotions"
    jobs = []
    for voice in a.voices.split(","):
        extra = {} if voice == "default" else {"voice": voice}
        jobs += [(f"{voice}/{emo}", text, extra, out / voice / f"{emo}.wav") for emo, text in EMOTIONS.items()]
    render_jobs(a, jobs)
    bench.log(f"wrote {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default="http://localhost:8000")
    common.add_argument("--extra", default='{"temperature": 0.8, "top_k": 50}')
    common.add_argument("--parallel", type=int, default=4)
    sub.add_parser("refs").set_defaults(func=cmd_refs)
    sub.add_parser("upload", parents=[common]).set_defaults(func=cmd_upload)
    sub.add_parser("showcase", parents=[common]).set_defaults(func=cmd_showcase)
    e = sub.add_parser("emotions", parents=[common])
    e.add_argument("--voices", default="default,us_andrew,gb_sonia,in_prabhat")
    e.set_defaults(func=cmd_emotions)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
