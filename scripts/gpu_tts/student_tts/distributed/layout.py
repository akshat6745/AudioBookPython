"""Stable private R2 keys. Payloads are immutable; manifests are the completion mark."""

from __future__ import annotations

import os

STYLETTS_RUNTIME = "styletts2-eye-v1"
STYLETTS_VERSION = "2026-10-20.1"
TAGGER_RUNTIME = "emotion-tagger-v1"
TAGGER_MODEL = "deberta3large_teacher2+deberta3_teacher2"
TAGGER_VERSION = "2026-10-20.1"
SOURCE_VERSION = "v1"
TAG_SCHEMA = "tag-rows/v1"
AUDIO_SCHEMA = "student-audio/v1"
SOURCE_SCHEMA = "novel-source/v1"
STYLETTS2_COMMIT = "5cedc71c333f8d8b8551ca59378bdcc7af4c9529"
STYLETTS2_REPO = "https://github.com/yl4579/StyleTTS2.git"

LEASE_TTL_S = 20 * 60
HEARTBEAT_S = 60
TAG_DEFAULT_BATCH = 32
AUDIO_DEFAULT_BATCH = 4
AUDIO_CPU_DEFAULT_BATCH = 1
BATCH_TOKENS = 1600
MAX_PHONEME_TOKENS = 510
SAMPLE_RATE = 24000
BITRATE = "64k"
TARGET_LUFS = -18.0
LIMITER = 0.84
STYLE_MIX_PROSODY = (0.7, 0.3)  # predicted, reference
STYLE_MIX_ACOUSTIC = (0.3, 0.7)
DIFFUSION_STEPS = 5
EMBEDDING_SCALE = 1
BANK_EMOTIONS = (
    "neutral",
    "affection",
    "amusement",
    "anger",
    "bitterness",
    "contemplation",
    "determination",
    "fear",
    "sadness",
    "surprise",
)

EXISTING_CHECKPOINT_KEY = "student_tts/styletts2-eye-v1/checkpoint/epoch_2nd_00005.pth"
VERIFIED_CHECKPOINT = {
    "key": EXISTING_CHECKPOINT_KEY,
    "bytes": 2252269594,
    "etag": "d94564407da728b03f35384b089b1183-269",
    "sha256": "55bd57ace19fec8b51e11d82824fc9f7177b95d0520aaaa78aae5bc8dfc09d67",
}

KNOWN_SHA256 = {
    "checkpoint/epoch_2nd_00005.pth": VERIFIED_CHECKPOINT["sha256"],
    "checkpoint/config_ft.yml": "791b53cdef39fe1595e30e998c4e5406dae3e8846372ad290a7931467dfa80b5",
    "asr/epoch_00080.pth": "fedd55a1234b0c56e1e8b509c74edf3a5e2f27106a66038a4a946047a775bd6c",
    "asr/config.yml": "f573495b4377524e224b1c73784052f20378c1d68734f38d67f3da32aaad970f",
    "jdc/bst.t7": "54dc94364b97e18ac1dfa6287714ed121248cfaac4cfd39d061c6e0a089ef169",
    "plbert/step_1000000.t7": "0714ff85804db43e06b3b0ac5749bf90cf206257c6c5916e8a98c5933b4c21e0",
    "plbert/config.yml": "e5eb4151f130ceae8dd2538f90aced3b66fcad5afa46724b36be435725decdde",
    "style_bank/bank.npz": "c7cb878be63a10e3a17b7c18c4e3e830eee8772f261433d21ac6e24fee18af6c",
    "style_bank/bank.json": "770194d093a991c4026d3aaeb4b28ccede9491152ee2dfdb70c20e9c5d586b18",
}

KNOWN_BYTES = {
    "checkpoint/epoch_2nd_00005.pth": 2252269594,
    "checkpoint/config_ft.yml": 2109,
    "asr/epoch_00080.pth": 94552811,
    "asr/config.yml": 481,
    "jdc/bst.t7": 21029926,
    "plbert/step_1000000.t7": 25185187,
    "plbert/config.yml": 915,
    "style_bank/bank.npz": 102664,
    "style_bank/bank.json": 45703,
}


def namespace() -> str:
    return os.environ.get("AUDIOBOOK_WORKER_NAMESPACE", "").strip("/")


def k(key: str) -> str:
    ns = namespace()
    key = key.lstrip("/")
    if ns:
        return f"{ns}/{key}"
    return key


def novel_index_key() -> str:
    return k("student_tts/novels/index.json")


def source_prefix(slug: str) -> str:
    return k(f"student_tts/novels/{slug}/source/{SOURCE_VERSION}/")


def source_manifest_key(slug: str) -> str:
    return source_prefix(slug) + "manifest.json"


def titles_key(slug: str) -> str:
    return source_prefix(slug) + "titles.json"


def chapter_text_key(slug: str, chapter: int) -> str:
    return source_prefix(slug) + f"chapters/{chapter:05d}.txt"


def paragraph_meta_key(slug: str, chapter: int) -> str:
    return source_prefix(slug) + f"paragraphs/{chapter:05d}.json"


def tag_prefix(slug: str) -> str:
    return k(f"student_tts/novels/{slug}/tags/{TAGGER_MODEL}/")


def tag_manifest_key(slug: str, chapter: int) -> str:
    return tag_prefix(slug) + f"chapter_{chapter:05d}.manifest.json"


def tag_blob_key(slug: str, digest: str) -> str:
    return tag_prefix(slug) + f"blobs/{digest}.jsonl"


def lease_key(slug: str, stage: str, chapter: int) -> str:
    if stage not in {"tag", "audio"}:
        raise ValueError(f"lease stage must be tag or audio, got {stage}")
    return k(f"student_tts/coord/{slug}/{stage}/chapter_{chapter:05d}/lease.json")


def audio_prefix(slug: str, chapter: int, *, versioned: bool = False) -> str:
    """Canonical prefix matches the app. Versioned prefix is used only when canonical bytes must not change."""
    base = k(f"audio-student/{STYLETTS_RUNTIME}/{slug}/")
    if versioned:
        return f"{base}runtimes/{STYLETTS_VERSION}/chapter_{chapter}/"
    return f"{base}chapter_{chapter}/"


def attempt_prefix(slug: str, stage: str, attempt_id: str, chapter: int) -> str:
    return k(f"student_tts/attempts/{slug}/{stage}/{attempt_id}/chapter_{chapter:05d}/")


def styletts_manifest_key() -> str:
    return k(f"student_tts/runtime/{STYLETTS_RUNTIME}/{STYLETTS_VERSION}/manifest.json")


def tagger_manifest_key() -> str:
    return k(f"student_tts/runtime/{TAGGER_RUNTIME}/{TAGGER_VERSION}/manifest.json")


def styletts_artifact_key(name: str) -> str:
    return k(f"student_tts/runtime/{STYLETTS_RUNTIME}/{STYLETTS_VERSION}/artifacts/{name}")


def tagger_artifact_key(name: str) -> str:
    return k(f"student_tts/runtime/{TAGGER_RUNTIME}/{TAGGER_VERSION}/artifacts/{name}")


def assert_publish_key_allowed(key: str) -> None:
    """Publish of source, tags, and runtime must not write listener audio."""
    bare = key
    ns = namespace()
    if ns and bare.startswith(ns + "/"):
        bare = bare[len(ns) + 1 :]
    if bare.startswith("audio-student/") or bare.startswith("audio/") or bare.startswith("covers/"):
        raise RuntimeError(f"refusing to publish into an audio/listener prefix: {bare}")
