# Emotion-Aware Narration — How to Rebuild This (Undone on 2026-09-04)

This documents a feature that was fully designed, implemented, tested, and
**then deliberately removed** from both `AudioBookPython` (backend) and
`audiobook-web` (frontend) at the user's request. Nothing about the *design*
was wrong — this is a "put it back exactly like it was" runbook, not a
lessons-learned doc.

**Read this first if you're the one reviving it. Section 1 (fastest path) is
almost certainly what you want** — full rebuild-from-scratch (Section 2) is
only useful if the backup archive described in Section 1 no longer exists.

---

## 0. What existed right before removal

- Two new local sibling services: `emotion-detector/` (port 6001) and
  `emotional-tts/` (port 6002), each a standalone FastAPI app with its own
  `.venv`, `requirements.txt`, `Dockerfile`, and test suite (41 + 37 tests,
  all green).
- A shared SQLite DB at `shared/emotion.db` — **fully analyzed for the
  entire Lord of the Mysteries novel**: 1,432/1,432 chapters, 108,416/108,416
  segments (~50 MB, ~1 hour of compute).
- Backend integration: 4 new endpoints on `AudioBookPython`
  (`/novel-with-emotional-tts`, `/emotional-voices`,
  `/novel-emotional-tts-prepare`, `/paragraph-with-emotional-tts`), a
  `use_emotional_tts` flag on `POST /download/chapter`, new settings
  (`EMOTION_DETECTOR_URL`, `EMOTIONAL_TTS_URL`, ...), and Docker Compose
  `profiles: ["emotional"]` service defs for `emotion-detector`,
  `emotional-tts`, and `ollama`.
- Frontend integration: an "Emotional narration (beta)" checkbox in
  `ChapterContentPage.tsx`, real per-paragraph emotional playback wired into
  `AudioPlayer.tsx` (Kokoro/Chatterbox voice picker, prepare-then-play flow),
  and supporting changes in `api.ts`, `ttsService.ts`, `types/index.ts`,
  `audioPlayerUtils.ts`.
- Full design rationale lived in `EMOTION_TTS_ARCHITECTURE.md` (20-section
  architecture doc: model comparison, DB schema, emotion taxonomy, caching
  strategy, Apple Silicon considerations, etc.) — also removed, but backed
  up (below).

---

## 1. Fastest path: restore from the backup archive (recommended)

Everything below was archived **before** deletion, specifically so this
could be restored without redoing any of the work:

```
~/Desktop/Projects/AudioBookPython-emotion-backup-20260904.tar.gz   (~31 MB)
```

Contents:
- `emotion-detector/` and `emotional-tts/` full source (code, tests,
  Dockerfiles, README, reference voice clips, sample-generation scripts) —
  **`.venv/`, `__pycache__/`, `.pytest_cache/` excluded** (reinstall from
  `requirements.txt`; venvs are multi-GB and trivially reproducible).
- `shared/emotion.db` — the complete, already-computed LOTM emotion
  analysis (108,416 segments). **`shared/audio_cache/` excluded** — this was
  leftover output from an audio-caching layer that was built then removed
  again per an earlier user request (see `.claude/PROGRESS.md`'s "Phase 6 …
  REMOVED" note, also in this archive) — not needed, cheap to regenerate.
- `EMOTION_TTS_ARCHITECTURE.md`, `prompt.md` (the original request that
  kicked this off), `.claude/PROGRESS.md` (detailed session-by-session
  history, bugs found/fixed, exact commands used).
- `patches-backend/backend_full_diff.patch` — the exact diff that was
  applied to `AudioBookPython`'s `README.md`, `app/api/download_v2.py`,
  `app/api/novels.py`, `app/core/settings.py`, `requirements.txt`,
  `docker-compose.yml` (against the commit that was HEAD when this was
  removed — check `git log` for `perf: parallelize novel/chapter/upload
  paths, pool D1 connections` to confirm you're on the same base before
  applying).
- `patches-frontend/frontend_full_diff.patch` — same, for
  `audiobook-web`'s `AudioPlayer.tsx`, `ChapterContentPage.tsx`, `api.ts`,
  `ttsService.ts`, `types/index.ts`, `audioPlayerUtils.ts`.

### Restore steps

```bash
cd ~/Desktop/Projects
tar -xzf AudioBookPython-emotion-backup-20260904.tar.gz -C /tmp/emotion-restore

# 1. Bring back the two services + shared DB
cp -r /tmp/emotion-restore/emotion-detector /tmp/emotion-restore/emotional-tts /tmp/emotion-restore/shared \
   AudioBookPython/

# 2. Bring back the doc/notes
cp /tmp/emotion-restore/EMOTION_TTS_ARCHITECTURE.md /tmp/emotion-restore/prompt.md AudioBookPython/
mkdir -p AudioBookPython/.claude && cp /tmp/emotion-restore/.claude/PROGRESS.md AudioBookPython/.claude/

# 3. Re-apply the backend code changes (from AudioBookPython/ root)
cd AudioBookPython
git apply /tmp/emotion-restore/patches-backend/backend_full_diff.patch
# You'll also need the untracked service-client files the patch doesn't
# cover (they were new files, not diffs) — copy them back explicitly:
#   app/services/emotion_client.py, emotional_narration.py,
#   emotional_tts_client.py, wav_utils.py
#   app/tests/test_emotion_client.py, test_emotional_narration.py,
#   test_emotional_tts_client.py, test_wav_utils.py
# These are ALSO in the tar under the same paths — copy from
# /tmp/emotion-restore/app/... into AudioBookPython/app/...

# 4. Re-apply the frontend changes
cd ../audiobook-web
git apply /tmp/emotion-restore/patches-frontend/frontend_full_diff.patch

# 5. Rebuild the two services' venvs
cd ../AudioBookPython/emotion-detector && python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cd ../emotional-tts && python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
# NOTE (hit during original build): a global ~/.pip/pip.conf pointed at an
# internal CodeArtifact index hangs on an auth prompt for these installs —
# use `PIP_CONFIG_FILE=/dev/null pip install -r requirements.txt` instead.
# Also: pin `setuptools<81` in this venv — chatterbox-tts's `perth`
# dependency needs `pkg_resources`, dropped in setuptools>=81.

# 6. Start everything (see Section 3 below) and verify against
#    .claude/PROGRESS.md's "Live-verified in browser" section for what
#    "working" looked like last time.
```

If `git apply` reports conflicts (likely, if other unrelated commits have
landed on either repo since removal), fall back to manually re-adding each
hunk from the `.patch` file — it's organized per-file with clear context.

---

## 2. Full rebuild from scratch (if the backup is gone)

This mirrors the actual implementation phases as they happened. Follow in
order; each phase's "done" bar is what was verified last time.

### Phase 1 — `emotion-detector` prototype (port 6001)

- New standalone FastAPI service in `emotion-detector/`.
- **Segmentation**: paragraph/dialogue-block granularity (not
  whole-chapter, not single-sentence) — see `app/core/segmentation.py`,
  `scene_split.py`, `text_cleanup.py`. **Important learned bug**: normalize
  curly quotes (`"`/`"`, `'`/`'`) to straight quotes *before* dialogue
  detection — `segmentation.normalize_quotes()` — real chapters had 0/74
  segments detected as dialogue without this.
- **Classification, two tiers**:
  - Tier 1 (always runs, fast): `SamLowe/roberta-base-go_emotions` (MIT
    license, GoEmotions taxonomy, HF `transformers` pipeline). Use
    `classify_batch`/`analyze_batch` — one HF pipeline call per chapter's
    worth of segments, not one call per segment (~12/sec → ~43/sec batched;
    this was a measured, not guessed, perf fix).
  - Tier 2 (escalation, ambiguous/dialogue segments only): local LLM via
    Ollama, model `qwen2.5:7b-instruct`. Gate behind
    `ENABLE_LLM_ESCALATION` env var, default it to fail open (fall back to
    Tier-1-only if Ollama isn't reachable — never block on this).
  - `app/core/taxonomy.py`: map GoEmotions' 27 labels down to a practical
    audiobook-narration taxonomy (emotion + intensity, not dozens of
    discrete categories). Known quirk: GoEmotions' "desire" → our
    "romantic" bucket can misfire on non-romantic urgent statements — low
    intensity in practice, flagged not fixed.
  - `app/core/speaker_attribution.py`: character-aware — attribute dialogue
    segments to a speaker using surrounding narration context.
- **API**: `POST /analyze` (single segment), `POST /analyze/book` (whole
  book, resumable — see Phase 2), `GET /analysis/{segment_id}`,
  `GET /health`.
- Test with: unit tests per module (`tests/test_segmentation.py`,
  `test_classifier.py`, `test_llm_escalation.py`, `test_pipeline.py`,
  `test_text_cleanup.py`, `test_book_pipeline.py`) — 41 tests total when
  complete.

### Phase 2 — DB persistence, resumability, versioning

- `app/db/schema.sql`, SQLite, WAL mode, at `shared/emotion.db` (shared
  with `emotional-tts` — same file, mounted from both services).
- Tables mirror: `novels → chapters → scenes → text_segments →
  emotion_analyses`, plus a `characters` table and an emotion
  model/version column on `emotion_analyses` so re-running with a new model
  doesn't destroy prior results.
- `text_segments` tracks a content hash so `_ensure_segmented` can detect
  "already segmented, skip" vs. "text changed, re-segment" — **and also
  check that scenes actually exist**, not just that the hash is unchanged
  (a robustness gap found via manual DB poking, fixed).
- `POST /analyze/book` must be resumable: if it dies mid-batch, a re-run
  picks up exactly where it left off, recomputing nothing already done.
  (Verified for real: a background analysis run died ~8 minutes in during
  the original build; the resumed run lost zero completed work.)

### Phase 3 — `emotional-tts` prototype (port 6002)

- New standalone FastAPI service in `emotional-tts/`.
- **Engine**: start with **Kokoro-82M** (Apache 2.0, `hexgrad/Kokoro-82M`,
  fast CPU inference, good baseline quality) — `app/engines/kokoro_adapter.py`.
- **Known ceiling, discovered during build**: Kokoro's "emotional"
  expressiveness is speed-only — no pitch/timbre change. Confirmed by
  A/B-listening to generated samples, not assumed. This is why Chatterbox
  was added as a second engine (see below) and made default.
- **Second engine: Chatterbox** (`resemble-ai/chatterbox`, MIT license,
  zero-shot voice cloning from ~5s reference audio, real `exaggeration`
  parameter that feeds the model's actual emotion conditioning, not just
  speed) — `app/engines/chatterbox_adapter.py`. `pip install
  chatterbox-tts==0.1.7`.
  - Gotchas hit installing it: (1) a global `~/.pip/pip.conf` pointed at an
    internal package index hangs on auth — use `PIP_CONFIG_FILE=/dev/null`;
    (2) `setuptools>=81` dropped `pkg_resources`, needed by Chatterbox's
    `perth` watermarking dependency — pin `setuptools<81` in the venv.
  - Chatterbox ships no named voice catalog (unlike Kokoro) — it's
    zero-shot only. Generate reference clips via the app's own edge-tts
    defaults so voice identity matches the non-emotional path:
    `scripts/generate_reference_voices_edge_tts.py` →
    `reference_voices/{af_bella,am_michael,ava,ryan}_ref.wav` (or
    equivalent names).
  - `app/core/style_mapping.py`: interpolate `exaggeration` (0.4 neutral →
    1.8 angry), `cfg_weight` (0.5 → 0.2), `temperature` (0.8 → 1.1) from a
    neutral anchor up to each emotion's tuned target as intensity goes
    0→1 — tuned by ear across three rounds of generated samples (see
    `scripts/generate_chatterbox_*.py` if you want to redo that listening
    pass), not guessed from docs.
  - Cost tradeoff to know going in: Chatterbox is ~13-20s/paragraph on CPU
    vs. Kokoro's near-instant — real latency, not yet mitigated by
    pre-generation/look-ahead buffering.
  - At the tuned range's extreme end (`exaggeration` >1.5, `cfg_weight`
    <0.25 — angry/fearful/surprised), expect increased risk of artifacts
    per Resemble AI's own guidance; no outright failures were hit in
    testing, but it's a real edge.
  - Make the default engine configurable/switchable
    (`ENABLE_CHATTERBOX` env var); fall back to Kokoro if reference clips
    are missing rather than crashing.
- **API**: `POST /synthesize` (single segment), a batch variant, `GET
  /voices`, `GET /voices/{voice_id}`, `GET /health`. No audio caching in
  the final version (see Phase 6 below for why) — always regenerate.
- 37 tests when complete (`tests/test_synth_service.py`,
  `test_style_mapping.py`, `test_voice_repository.py`,
  `test_character_repository.py`, `test_tts_generation_repository.py`,
  `test_schemas.py`).

### Phase 4 — Character voice registry

- `voices` table: `id`, `engine`, `display_name`, `reference_audio_path`,
  `is_narrator_default`, `is_dialogue_default` (the latter added via an
  idempotent `ALTER TABLE` migration in `db/connection.py::_migrate()` so
  it's safe to run against an already-populated DB).
- On startup, register two Kokoro voices (`kokoro:af_bella` narrator
  default, `kokoro:am_michael` dialogue default) and, if
  `ENABLE_CHATTERBOX=true` and reference clips exist, two Chatterbox voices
  (`chatterbox:ava`, `chatterbox:ryan`) as the new defaults instead — Kokoro
  stays registered as a manually-selectable fallback.
- `POST /characters/voice` to assign a specific character (e.g. "Klein
  Moretti") to a specific voice, overriding the narrator default for their
  dialogue lines. (Built and tested; never actually populated for LOTM —
  everyone used the default narrator voice in the final state.)

### Phase 5 — AudioBookPython integration

New files in `app/services/`: `emotion_client.py` (HTTP client for
`emotion-detector`), `emotional_tts_client.py` (HTTP client for
`emotional-tts`), `emotional_narration.py` (orchestrates: fetch/trigger
analysis → resolve character voice → call TTS → return audio bytes +
segment metadata), `wav_utils.py` (concatenate multiple WAV clips into one
correctly-headed file — **naive byte concatenation of WAV headers plays
wrong in most players**, don't skip this).

New settings in `app/core/settings.py`:
```python
EMOTION_DETECTOR_URL: str = "http://localhost:6001"
EMOTIONAL_TTS_URL: str = "http://localhost:6002"
EMOTIONAL_ANALYSIS_POLL_INTERVAL_S: float = 2.0
EMOTIONAL_ANALYSIS_TIMEOUT_S: float = 600.0
```

New endpoints in `app/api/novels.py`:
- `GET /novel-with-emotional-tts` — whole-chapter, buffers full audio
  before responding (unlike the streaming edge-tts path).
- `GET /emotional-voices` — proxies `emotional-tts`'s `GET /voices` (the
  browser can't reach port 6002 directly).
- `GET /novel-emotional-tts-prepare` — call once per chapter before any
  per-paragraph playback, so analysis isn't triggered piecemeal.
- `POST /paragraph-with-emotional-tts` — one paragraph, on demand; takes
  `text` directly from the caller rather than re-fetching, so it still
  works for paragraphs emotion-detector's segmentation dropped (e.g.
  scene-break markers) — falls back to a neutral read in that case.

`app/api/download_v2.py`: add `use_emotional_tts` /
`emotional_narrator_voice_id` / `emotional_dialogue_voice_id` /
`known_characters` to `DownloadRequest`; branch `process_chapter_download`
into `_process_edge_tts_chapter` (the original logic, moved out verbatim —
don't change its behavior) and a new `_process_emotional_chapter` (produces
per-segment `.wav` files, not per-paragraph `.mp3`, since emotion-detector's
segmentation doesn't line up 1:1 with the original paragraph split).

`app/api/download_v2.py`'s file-serving endpoint also needs to serve
`.wav` with `media_type="audio/wav"` (not force everything to
`audio/mpeg`).

`requirements.txt`: add `httpx` (used by the two new HTTP clients).

`docker-compose.yml`: add `profiles: ["emotional"]` service defs for
`emotion-detector` (port 6001, depends on `ollama`), `emotional-tts` (port
6002), and `ollama` (port 11434, for Tier-2 LLM escalation) — **not**
started by a plain `docker compose up`; bring up with `docker compose
--profile emotional up`. Share one `shared_data` volume mounted at
`/shared` in both `emotion-detector` and `emotional-tts` for the DB +
formerly the audio cache.

`README.md`: document the above (see git history / the backup patch for
exact wording).

### Phase 6 — Caching (built, then explicitly reverted — do not redo unless asked)

An audio-caching layer (`emotional-tts/app/core/audio_cache.py`,
`db/tts_generation_repository.py`, `api/audio.py`) was built, keyed on
`text hash + voice + emotion + intensity + model version`. **The user then
asked for it to be removed** — every `/synthesize` call regenerates fresh
audio and returns bytes directly instead. If those archived files still
exist in a future codebase, they're intentionally unwired (not imported by
`main.py`) — re-wire `synth_service.py` to bring caching back, but confirm
with the user first since it was a deliberate reversal, not an oversight.

### Phase 7 — Docker Compose

Validate with `docker compose config` (catches YAML/interpolation errors
without pulling images). Actually running `docker compose up --profile
emotional` end-to-end was **never done** in the original build — a real gap
if you want production-parity confidence, not just config validity.

**Apple Silicon note carried over from the design doc**: prefer *native*
`uvicorn` execution for `emotion-detector`/`emotional-tts` on macOS/Apple
Silicon — Docker Desktop/Rancher Desktop on macOS runs Linux containers in
a VM with no MPS passthrough, so GPU acceleration is unavailable inside the
container either way; running natively at least gets you full CPU speed and
optionally MPS. Docker Compose is still useful for Linux hosts/CI.

### Phase 8 — Real-world testing (Lord of the Mysteries) + frontend wiring

- Ran full-novel analysis: 1,432 chapters, 108,416 segments, ~1 hour,
  verified against the DB directly (not just script logs).
- Frontend (`audiobook-web`, separate repo):
  - `ChapterContentPage.tsx`: `useEmotionalTts` state + an "Emotional
    narration (beta)" checkbox next to the Download button; download
    branches to `generateChapterAudioEmotional` (`.wav` output) vs. the
    existing edge-tts path (`.mp3`).
  - `AudioPlayer.tsx`: on `useEmotionalTts`, calls
    `TTSService.prepareChapterEmotional` once per chapter open (gates
    per-paragraph synthesis on completion via `isEmotionalReady`), fetches
    the Kokoro/Chatterbox voice catalog via `fetchEmotionalVoices`
    (defaulting from `is_narrator_default`/`is_dialogue_default`), and
    swaps the Narrator/Dialogue `<select>`s to show that catalog instead of
    the 14-voice edge-tts list when emotional mode is on.
  - `types/index.ts` / `audioPlayerUtils.ts`: `EnhancedParagraph` gains
    `originalIndex` (the paragraph's position in the chapter's *raw* array,
    surviving upstream filtering of empty paragraphs) so per-paragraph
    requests address the correct `source_paragraph_index` in
    emotion-detector's DB.
  - `api.ts` / `ttsService.ts`: `generateChapterAudioEmotional`,
    `prepareEmotionalTts`, `generateParagraphAudioEmotional`,
    `fetchEmotionalVoices`.
- Live-verified via Playwright against a real Chrome instance: checkbox
  works, `/paragraph-with-emotional-tts` returns real `audio/wav`, the
  `<audio>` element actually advances (`paused: false`, `currentTime`
  increasing).

### Phases 9-10 — Not reached

Haunting Adeline (second novel) was never analyzed/tested, and there was no
dedicated optimization pass beyond the classifier batching improvement
folded into Phase 1. Both remain open if this is rebuilt.

---

## 3. How to start everything once rebuilt

```bash
# emotion-detector (port 6001)
cd emotion-detector && source .venv/bin/activate
ENABLE_LLM_ESCALATION=false uvicorn app.main:app --host 0.0.0.0 --port 6001 &
# (set true + have `ollama pull qwen2.5:7b-instruct` done first, for Tier-2)

# emotional-tts (port 6002)
cd emotional-tts && source .venv/bin/activate
ENABLE_CHATTERBOX=true uvicorn app.main:app --host 0.0.0.0 --port 6002 &

# AudioBookPython backend
cd AudioBookPython
python3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8090 &
# use an explicit python3.11 binary if a stale .testEnv/ (py3.9) precedes
# it on PATH for non-interactive shells

# Frontend
cd ../audiobook-web && npm start &
```

Health checks: `curl localhost:6001/health`, `:6002/health`,
`:8090/health`.

---

## 4. Known gaps to carry forward (not fixed, not urgent, but real)

- Tier-2 LLM escalation was never tested for real (Ollama wasn't set up).
- No character-specific voices were ever assigned for LOTM.
- Haunting Adeline untested.
- `docker compose --profile emotional up` never actually run end-to-end.
- GoEmotions "desire" → "romantic" taxonomy mapping can misfire (low
  intensity, low impact).
- No critical-listening quality pass across many chapters — Phase 8 proved
  the pipeline *works*, not that narration quality was judged rigorously.
