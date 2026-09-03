# Backend TTS change — edge-tts → Piper/Kokoro

**Companion to** the client design in
`audiobook-app-flutter/OFFLINE_FIRST_ARCHITECTURE.md` (§5) and
`OFFLINE_TTS_POC_PLAN.md` (Part B).

## Why
The app is moving to **on-device** Piper/Kokoro (via `sherpa_onnx`) for fully
offline playback. To keep the **deployed website** and any pre-generated audio
sounding identical to the app, the backend should synthesize with the **same
ONNX models** instead of `edge-tts` (which is a Microsoft cloud service).

## What changes
- **`app/api/tts.py`** — replace only the per-segment synthesizer. Keep:
  - the dual-voice segmentation (`text_to_speech_dual_voice`), and
  - the concurrency cap `_TTS_SEMAPHORE`.
- **`requirements.txt`** — add `sherpa-onnx` (or a Piper binding). `edge-tts`
  can stay as an online fallback initially, then be retired.
- Optional: pre-generate MP3 per chapter and upload to R2 (`chapters.r2_audio_path`)
  for the app's download path.

## How — prefer the native Python library (no shell)
Synthesize in-process with `sherpa-onnx` (Python). This avoids spawning a
subprocess entirely and is the recommended path (per the project security
guideline: *prefer native libraries over shell commands*).

## If a CLI (e.g. `piper`) is used instead — command-injection-safe pattern
Only if a library route isn't viable. Pass user **text via stdin** (never as a
shell argument), resolve the model from an **internal allowlist** (never user
input), use list args, `shell=False`, and a timeout:

```python
import subprocess

# Voice name → model path is INTERNAL config, not user-controlled.
VOICE_MODELS = {
    "narrator": "/models/en_US-narrator.onnx",
    "dialogue": "/models/en_US-dialogue.onnx",
}

def synth(text: str, voice: str) -> bytes:
    model = VOICE_MODELS.get(voice)          # allowlist lookup
    if model is None:
        raise ValueError("Unknown voice")
    result = subprocess.run(
        ["piper", "--model", model, "--output_file", "-"],
        input=text.encode(),                 # user text via stdin, never the shell
        capture_output=True,
        shell=False,
        timeout=60,
    )
    return result.stdout
```

- One defense is enough: list args + `shell=False` is sufficient — don't also
  add regex/blocklist escaping.
- Do **not** return exception details/tracebacks to clients; log server-side and
  return a generic error (as `app/main.py`'s global handler already does).

## Acceptance
- Same voice as the on-device app (same model files).
- Acceptable real-time factor on the deployment tier.
- No shell-injection surface introduced.
