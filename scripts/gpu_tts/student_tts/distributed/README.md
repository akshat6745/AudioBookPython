# Distributed student audiobook worker

This worker tags a private novel and renders StyleTTS2 audio. Cloudflare R2 is the source of truth. A laptop only caches files. Two laptops, or two workers on one laptop, claim chapters with a conditional put, so a chapter is tagged or rendered once.

Tagging and audio are independent. Resume lists the chapters you selected and asks R2 what is already valid. It does not trust a local progress file, and it does not stop at the first gap.

```bash
./bootstrap.sh
./audiobook-worker run --novel "The Eye of the World" --stage tag --workers 4 --batch-size 32 --validate
./audiobook-worker validate --novel "The Eye of the World" --stage tag
./audiobook-worker run --novel "The Eye of the World" --stage audio --workers 2 --batch-size 4 --validate
./audiobook-worker validate --novel "The Eye of the World" --stage audio
```

The same commands work as `python -m scripts.gpu_tts.student_tts.distributed ...` from the repository root. `./audiobook-worker` uses `.venv-r2worker` when that virtualenv exists.

Do not start `--stage audio` for The Eye of the World while the older local renderer is still uploading those chapters. That older process does not take these leases.

## Architecture

R2 holds:

- the versioned StyleTTS2 runtime, plus the existing production checkpoint key that runtime references
- the versioned DeBERTa emotion-tagger runtime
- the private novel source and the novel index
- per-chapter tag manifests and content-addressed tag JSONL
- coordination leases
- audio objects and the audio manifest that marks a chapter complete

`--stage tag` writes tag manifests for chapters that are missing or stale. It does not download or load StyleTTS2, and it does not write audio.

`--stage audio` (alias `--stage render`) writes audio only for chapters whose R2 tag manifest validates. It does not run the tagger. If any requested chapter lacks a valid tag manifest, it stops before loading StyleTTS2 and prints the incomplete chapters. `--allow-partial-tags` renders the chapters that already have valid tags.

`--stage all` runs tag, then audio, unless tag setup failed (exit 4). Audio still refuses to load StyleTTS2 when the requested tags are not valid. Validation with `--stage all` checks source, tags, and audio.

A claim is `PutObject` with `If-None-Match: *`. Exactly one writer wins. The worker identity is `host/run-id/index`. Heartbeats refresh the lease every 60 seconds with `If-Match`. Another worker may take the lease over after 20 minutes, again with `If-Match`. A timed-out write is kept only when `HeadObject` shows this worker still owns it. Ctrl-C marks the lease `released` with `If-Match` when this process still owns it. The lease object is not deleted. The chapter is finished only when the manifest is written last and its payload hashes match. Losing the local cache does not undo a finished chapter.

Style-bank emotions are neutral, affection, amusement, anger, bitterness, contemplation, determination, fear, sadness, and surprise. Any other label uses that voice's neutral vectors. New audio is 24 kHz mono Opus at 64 kbps (`libopus`), with a loudness target of -18 LUFS and a limiter at 0.84. Prosody mix is 0.7 predicted / 0.3 reference, and acoustic mix is 0.3 predicted / 0.7 reference. The diffusion path uses 5 noise levels.

## Security and prerequisites

S3 access needs all four of these settings. A Cloudflare API token does not sign these requests. Create a bucket-scoped R2 access key. Never commit the values, and never pass them as flags. There is no `--secret` or `--api-token` flag.

| Name | Placeholder |
| --- | --- |
| `R2_ENDPOINT_URL` | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_BUCKET_NAME` | `<bucket-name>` |
| `R2_ACCESS_KEY_ID` | `<access-key-id>` |
| `R2_SECRET_ACCESS_KEY` | `<secret-access-key>` |

`audiobook-worker` reads the process environment first, then the first file that exists among `$AUDIOBOOK_R2_ENV`, `./.env`, and `~/.config/audiobook-worker/r2.env`. `.env` is gitignored. The commands call the loader with prompting disabled, so a missing name exits 4 instead of asking. The loader can use a hidden prompt only when a caller leaves prompting on and stdin is a terminal.

Python 3.11 is required (`python3.11` on `PATH`). `ffmpeg` and `espeak-ng` must be on `PATH`. Encoding uses ffmpeg's `libopus` encoder. `./bootstrap.sh` installs the inference stack from public PyPI with `PIP_CONFIG_FILE=/dev/null`. It does not use CodeArtifact.

Model weights and private novel text stay in the private R2 bucket. Do not put them in Git, Git LFS, or a public release. The StyleTTS2 checkpoint is the private distilled Eye student model (the teacher was Higgs). Higgs weights, Qwen, the training set, and the virtualenv are not runtime uploads. This is how the bucket is operated, not legal advice.

## Hardware

Device order is CUDA, then Apple Silicon MPS, then CPU. Override with `--device cuda`, `--device mps`, or `--device cpu`. That covers an NVIDIA Linux machine, Apple Silicon, and CPU. Windows native is not supported. Run Ubuntu on WSL2 and use the Linux commands.

`./bootstrap.sh` prints an install line when a tool is missing. On macOS that is `brew install` plus the missing names (`ffmpeg`, `espeak-ng`, or both). On Ubuntu, Debian, and WSL2 the printed line is `sudo apt-get install ffmpeg libopus0 espeak-ng`.

Python 3.11 on macOS is `brew install python@3.11`. On Ubuntu or WSL2 it is `sudo apt-get install python3.11 python3.11-venv`.

Output from CUDA, MPS, and CPU is not bit-identical. The worker that wins the lease owns the upload. A later worker does not replace those bytes when the size or hash differs.

On the M3 Pro that produced the Eye audio, the measured audio setting was one worker and batch 4. Several audio workers on one GPU each load StyleTTS2. That can raise memory use and swap, and it can be slower than one worker. `--workers 2` is a valid command; it is a poor fit for that machine.

## Checkout and bootstrap

```bash
git clone https://github.com/akshat6745/AudioBookPython.git
cd AudioBookPython
git fetch origin student-tts-r2-worker
git checkout student-tts-r2-worker
```

From a checkout that already has the branch:

```bash
git fetch origin student-tts-r2-worker
git checkout student-tts-r2-worker
```

Put the four R2 variables in `./.env` using the placeholder names above, then:

```bash
./bootstrap.sh
```

`bootstrap.sh` checks Python 3.11, `ffmpeg`, and `espeak-ng`, creates `.venv-r2worker`, installs [requirements-inference.txt](requirements-inference.txt) from public PyPI, and runs `setup`. Setup downloads the versioned StyleTTS2 and tagger manifests, checks each file's size and SHA256, clones StyleTTS2 at `5cedc71c333f8d8b8551ca59378bdcc7af4c9529`, applies the inference patch, and writes `config.runtime.yml`. A failed hash deletes the partial file.

The production checkpoint was already in R2 at `student_tts/styletts2-eye-v1/checkpoint/epoch_2nd_00005.pth` (2,252,269,594 bytes). Its ETag is multipart, so `publish-runtime` streams the object and compares SHA256 with the audited digest, then references that key. It does not upload a second copy. `setup` downloads that same object into the local cache and checks the size and SHA256 there.

Cache directory: `~/.cache/audiobook-worker`, or `$AUDIOBOOK_WORKER_CACHE`.

Setup alone:

```bash
./audiobook-worker setup
```

## Register a novel

`publish-novel` reads a local directory. It does not download a book from the web. The directory contains `titles.json` and `chapters/00001.txt` (five-digit chapter names). Optional tags are `00001.jsonl` beside `--tags-dir`, one JSON object per paragraph.

```bash
./audiobook-worker publish-novel \
  --name "Novel Title" \
  --source-dir /path/to/book \
  --tags-dir /path/to/book/tags \
  --chapters 1-10
```

`--name`, `--source-dir`, and `--chapters` are required. `--tags-dir` and `--slug` are optional. The slug defaults to a lowercase hyphenated form of the name. The command refuses to run while `AUDIOBOOK_WORKER_NAMESPACE` is set, and it refuses to write `audio-student/`, `audio/`, or `covers/` keys.

A name-only run works only after that registration. An unknown name exits 4 and prints the `publish-novel` remedy.

The Eye of the World is already registered as `the-eye-of-the-world`, chapters 1–54. Chapter 55 is the preview and is excluded. Passing it to `publish-novel` for this slug drops it and prints the exclusion. Do not register it to make `run --novel "The Eye of the World"` work.

## Staged workflow

Recommended order for The Eye of the World. Run these from the repository root after `./bootstrap.sh`.

```bash
./audiobook-worker run --novel "The Eye of the World" --stage tag --workers 4 --batch-size 32 --validate
./audiobook-worker validate --novel "The Eye of the World" --stage tag
./audiobook-worker run --novel "The Eye of the World" --stage audio --workers 2 --batch-size 4 --validate
./audiobook-worker validate --novel "The Eye of the World" --stage audio
```

See the hardware note before using two audio workers on one GPU. On the M3 Pro, prefer:

```bash
./audiobook-worker run --novel "The Eye of the World" --stage audio --workers 1 --batch-size 4 --validate
```

`--stage all` is the default for `run`, `validate`, and `status`. These two are the same all-in-one run, with a validation gate at the end:

```bash
./audiobook-worker run --novel "The Eye of the World" --stage all --validate
./audiobook-worker --novel "The Eye of the World" --validate
```

`./audiobook-worker --novel "The Eye of the World"` is `run` with `--stage all` and no post-gate.

### Workers and batch size

`--workers N` is how many local worker processes run the selected stage. The default is 1. It is not the model batch. Each worker has its own temp directory and log. They share the R2 lease. For `validate` and `status`, `--workers` only parallelizes HEAD, GET, and hash checks. Those commands do not take a lease.

`--batch-size N` is the model batch for the selected stage. Tagging counts paragraphs in one DeBERTa forward pass. Audio counts TTS chunks in one forward pass, still limited by the 1600-token budget. If you omit it, tagging uses 32 and audio uses 4, or 1 when the audio device is CPU. `--tag-batch-size` and `--audio-batch-size` set one stage when `--stage all` runs both. An explicit value is used as given and is not raised. A saved profile can lower only the default, not an explicit flag.

On out-of-memory the chapter retries at `floor(N/2)` down to 1. The batch is never increased. The size that succeeded is written to that worker's `batch-profile.json`. The next command reads `~/.local/state/audiobook-worker/batch-profile.json` only when you omit `--batch-size`. `--reset-batch-profile` ignores the saved default for the current command. It does not delete the file.

### Chapter selection and reports

`--chapters 1-54` and `--chapters 1,2,5-7` select chapters for `run` and `validate`. The source manifest is the authority. A chapter outside it exits 4. The scan is the whole selection: valid chapters 1–18, missing 19, and valid 20 processes only 19.

`--allow-partial-tags` is a `run` flag. Without it, audio stops when any requested chapter is untagged. With it, audio renders the chapters whose tags already validate and leaves the others untouched.

`--report-json PATH` is accepted by `run`, `validate`, and `status`. It writes chapter statuses and counts. Credential values are redacted before the file is written.

## Validation and exit codes

`validate` and `status` load no StyleTTS2 or tagger weights. They read manifests and compare payload size and SHA256. `run --validate` does the stage, then the same check, and exits non-zero when the gate fails.

Statuses:

| Status | Meaning |
| --- | --- |
| `valid` | Fingerprints match and the payload hash matches. |
| `legacy_valid` | Older Eye audio whose manifest schema is absent or `styletts2-eye-v1/legacy`, and whose listed files match size and SHA256. Treated as finished audio. |
| `missing` | No manifest, no live lease, and no partial objects. |
| `leased` | A live lease. In progress. |
| `publishing` | `publish.json` was elected and the audio manifest is not complete. In progress. |
| `stale` | A dependency fingerprint changed. Tagging or a versioned audio rewrite can replace this. |
| `hash_mismatch` | An object exists and its size or SHA256 does not match the manifest. |
| `conflict` | A manifest without its payload, objects without a manifest, or bytes that must not be replaced. |
| `invalid` | Not JSON, a bad line count, a missing tag field, or an unsafe audio file name. |
| `skipped` | Counted only when a report row uses that status. The classifiers above do not emit it. |

Tag fingerprints are schema `tag-rows/v1`, tagger `deberta3large_teacher2+deberta3_teacher2`, tagger runtime `emotion-tagger-v1`, runtime version `2026-10-20.1`, the source chapter SHA256, and the paragraph count. The JSONL must have that many lines, and each line needs `emotion`, `style`, `sfx`, `confidence`, `style_conf`, and `sfx_conf`.

Audio fingerprints, for a non-legacy manifest, are schema `student-audio/v1`, the source SHA256, runtime `styletts2-eye-v1`, runtime version `2026-10-20.1`, the audited checkpoint SHA256, the tagger id, and the tag manifest SHA256. Legacy audio skips that fingerprint check and still checks every listed file.

Exit codes:

| Code | When |
| --- | --- |
| 0 | Complete (`valid`, `legacy_valid`, or an empty selection). |
| 1 | Unexpected failure. |
| 2 | Missing, leased, or mid-publish. |
| 3 | Invalid, stale, hash mismatch, or conflict. If both incomplete and invalid rows are present, the exit is 3. |
| 4 | Setup, credentials, an unregistered novel, a missing source manifest, or a chapter outside the source manifest. |
| 130 | Interrupted (Ctrl-C). |

## Interruption and resume

If tags through chapter 18 validate, start the tag stage again. It lists every selected chapter in R2. Chapter 19 is tagged when it is missing or stale, including when chapter 20 is already valid. Chapters that already validate are not claimed.

```bash
./audiobook-worker validate --novel "The Eye of the World" --stage tag --chapters 1-54
./audiobook-worker run --novel "The Eye of the World" --stage tag --chapters 1-54 --workers 4 --batch-size 32 --validate
```

Audio does not start until the requested tag manifests validate, unless you pass `--allow-partial-tags`. Resume for audio follows the audio manifests, not the tag progress file and not the local cache. Deleting `~/.cache/audiobook-worker` is safe after a chapter's manifest is in R2. The next `setup` downloads the runtimes again and rechecks size and SHA256.

Nothing is overwritten with different bytes. A tag JSONL key is the payload SHA256. A different tag manifest for the same chapter is a conflict. Canonical audio that already matches its sizes and hashes is left as `legacy_valid`, including Eye chapters published by the older renderer. A size or hash mismatch is `hash_mismatch` or `conflict` (exit 3). A stale `student-audio/v1` manifest is written under the versioned prefix below instead of replacing the canonical keys.

## R2 layout

Versions below are the strings in the manifests published on 2026-10-20 (`2026-10-20.1`). Payloads are immutable. The manifest is the completion mark.

| What | Key |
| --- | --- |
| StyleTTS2 runtime manifest | `student_tts/runtime/styletts2-eye-v1/2026-10-20.1/manifest.json` |
| StyleTTS2 auxiliary files | `student_tts/runtime/styletts2-eye-v1/2026-10-20.1/artifacts/<name>` |
| Existing checkpoint (referenced, not duplicated) | `student_tts/styletts2-eye-v1/checkpoint/epoch_2nd_00005.pth` |
| Tagger runtime manifest | `student_tts/runtime/emotion-tagger-v1/2026-10-20.1/manifest.json` |
| Tagger files | `student_tts/runtime/emotion-tagger-v1/2026-10-20.1/artifacts/...` |
| Novel index | `student_tts/novels/index.json` |
| Novel source | `student_tts/novels/<slug>/source/v1/manifest.json`, `titles.json`, `chapters/00001.txt` |
| Tag manifest | `student_tts/novels/<slug>/tags/deberta3large_teacher2+deberta3_teacher2/chapter_00001.manifest.json` |
| Tag JSONL | `student_tts/novels/<slug>/tags/deberta3large_teacher2+deberta3_teacher2/blobs/<sha256>.jsonl` |
| Lease | `student_tts/coord/<slug>/<stage>/chapter_00001/lease.json` |
| Upload attempt | `student_tts/attempts/<slug>/<stage>/<attempt-id>/chapter_00001/` |
| Canonical audio | `audio-student/styletts2-eye-v1/<slug>/chapter_<n>/` |
| Versioned audio, used when canonical bytes must not change | `audio-student/styletts2-eye-v1/<slug>/runtimes/2026-10-20.1/chapter_<n>/` |

`<stage>` is `tag` or `audio`. Lease and attempt chapter numbers are five digits. Canonical and versioned audio chapter numbers are not padded. Audio object names are `title.opus`, `<index>.opus`, and `chapter.opus`, then `manifest.json`. New audio is uploaded to the attempt prefix, `publish.json` is elected with `If-None-Match`, final objects are copied with `If-None-Match`, and the manifest is last.

Object counts from the 2026-10-20 delivery listing. They were not recounted for this document. Existing audio was listed, not rewritten, and no audio worker was started.

| Prefix | Objects | Bytes |
| --- | ---: | ---: |
| `student_tts/runtime/styletts2-eye-v1/2026-10-20.1/` (auxiliary files and manifest; checkpoint not included) | 9 | 140,922,098 |
| Existing checkpoint key, referenced and not re-uploaded | 1 | 2,252,269,594 |
| `student_tts/runtime/emotion-tagger-v1/2026-10-20.1/` | 25 | 2,476,613,287 |
| `student_tts/novels/the-eye-of-the-world/` (source and tags, chapters 1–54) | 164 | 2,543,157 |
| `student_tts/novels/index.json` | 1 | 616 |
| Existing Eye audio (`audio-student/...`, 54 manifests) | 6,151 | 1,793,116,396 |

Source and tag validation for Eye chapters 1–54 both returned 54 valid at that delivery. A style-bank download matched its audited SHA256 (45,703 bytes). The Opus files were not downloaded again to re-hash them.

## Operations

```bash
./audiobook-worker run --help
./audiobook-worker validate --help
./audiobook-worker status --help
./audiobook-worker setup --help
./audiobook-worker publish-runtime --help
./audiobook-worker publish-novel --help
```

A leading `--` is read as `run`, so `./audiobook-worker --help` prints `run` help rather than the subcommand list. With no arguments the process prints `choose run, validate, status, setup, publish-runtime, or publish-novel` and exits 4. Help itself prints the usage above and then exits 4 as well: argparse uses status 0, and the worker returns that 0 as the setup code. The usage text is still the command reference. `status` is `validate`. `run --status` is also validate-only and does not tag or render.

Stop a run with Ctrl-C. The parent sends SIGINT to worker processes and exits 130. An owned lease is marked `released`. If the network write fails, the lease stays until the 20-minute TTL and another worker can take it. Do not delete the lease object.

Logs for a real R2 run are printed as `[wN]` and appended to:

```text
~/.local/state/audiobook-worker/runs/<run-id>/w<index>/worker.log
```

That directory also gets the worker's `batch-profile.json`. Override the state directory with `AUDIOBOOK_WORKER_STATE`. There is no `cleanup` subcommand. Removing `~/.cache/audiobook-worker` only drops the local runtime cache; the next `setup` verifies the downloads again. Old `runs/` directories are local logs. Encode work under the system temp directory uses the prefix `audiobook-ch<chapter>-` and is removed when the chapter function returns. Do not delete R2 objects to retry a chapter. A hash or size mismatch is a conflict.

A machine-readable report:

```bash
./audiobook-worker validate --novel "The Eye of the World" --stage tag --report-json eye-tag-report.json
```

### Publish a runtime

`publish-runtime` is the admin upload. It checks audited hashes, reuses the existing checkpoint after the streamed SHA256 check, and uploads missing auxiliary files and the tagger bundle. A runtime manifest that already differs is left in place. The one replacement is a tagger manifest that still lists Hugging Face `/.cache/` paths; those cache objects are deleted and the manifest is rewritten with `If-Match`. Current uploads keep the student config, `model.safetensors`, and tokenizer files (`config.json`, `tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`, `spm.model`, `added_tokens.json`). They do not list the snapshot cache.

Do not pass a virtualenv, Higgs or Qwen weights, or the training set. They are not arguments, and they must not be added to Git or to these prefixes. The command also refuses `AUDIOBOOK_WORKER_NAMESPACE` and does not write listener audio keys.

```bash
./audiobook-worker publish-runtime \
  --checkpoint /path/to/epoch_2nd_00005.pth \
  --config /path/to/config_ft.yml \
  --asr /path/to/epoch_00080.pth --asr-config /path/to/asr/config.yml \
  --jdc /path/to/bst.t7 \
  --plbert /path/to/step_1000000.t7 --plbert-config /path/to/plbert/config.yml \
  --bank-npz /path/to/bank.npz --bank-json /path/to/bank.json \
  --deberta-large /path/to/deberta3large_teacher2 \
  --deberta-base /path/to/deberta3_teacher2
```

Every flag above is required.

## Tests and verification limits

At delivery of the worker implementation (`dee6762a590468de33f3f73106b5f3861f87821a`), the suite was 1394 passed and 1 skipped, including the existing postfx and Higgs tests. Worker coverage includes:

- leases: one winner, heartbeat, stale takeover, timeout-before-write versus timeout-after-write
- chapter ranges and gaps, including a missing chapter between valid chapters
- validation statuses, hash mismatch, and manifest-last publish
- tag, audio, validate, and CLI imports that do not load `tts_engine`, `tagger_model`, or `audio_render`
- audio preflight returning exit 2 before StyleTTS2 or the tagger is imported
- batch fallback that only decreases, and the Eye preview chapter being excluded

Those tests live in [tests/test_r2_worker.py](../../../../tests/test_r2_worker.py) and [tests/test_coordination.py](tests/test_coordination.py).

This was not a full second-machine run. `./bootstrap.sh` was not executed on the second machine, or on the Mac that was already rendering, because that install loads another copy of the model. A StyleTTS2 one-line smoke was skipped for the same contention. Production Eye audio was listed (54 manifests, 6,151 objects) and was not downloaded again for a hash check. Do not read this document as an end-to-end validation on a second machine.

A later commit on the same branch, `f6f8c9a4586bd6d300d167a361d1143a0b318a39`, keeps Hugging Face snapshot caches out of the tagger manifest. The object counts above are the delivery listing, not a new inventory after that commit.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Missing `R2_ACCESS_KEY_ID` or a complaint that an API token is not enough | Create a bucket-scoped R2 S3 key. Set the four `R2_*` names in the environment or a gitignored env file. A Cloudflare API token will not authenticate these calls. |
| Exit 4 and "not registered in the private R2 novel index" | Register a local source with `publish-novel`. A novel name does not fetch the book. |
| "refusing to load StyleTTS" or exit 2 from the audio stage | Finish `--stage tag` and `validate --stage tag` first, or pass `--allow-partial-tags` to render only chapters that already have valid tags. |
| A chapter stays `leased` | The owner heartbeats for up to 20 minutes. Ctrl-C on that machine releases it when the release write succeeds. After the TTL, the next worker takes the lease. Leave the lease object in place. |
| Batch falls, or the GPU starts swapping | Out-of-memory retry halves the batch down to 1. Use fewer `--workers`. On the M3 Pro, use one audio worker and `--batch-size 4`. |
| `ffmpeg` or `espeak-ng` missing | Install the packages in the hardware section and rerun `./bootstrap.sh`. On Windows, use WSL2 Ubuntu. |
| Exit 3, `hash_mismatch`, or `conflict` | The manifest and the bytes disagree, or different bytes already occupy the key. The worker will not overwrite them. Do not delete production audio to force a retry. |
| Wrong device | `--device auto` is CUDA, then MPS, then CPU. Pass `--device cpu` (or `cuda` or `mps`) to override. CPU audio defaults to batch 1. |

## Delivery

The worker implementation is commit [`dee6762a590468de33f3f73106b5f3861f87821a`](https://github.com/akshat6745/AudioBookPython/commit/dee6762a590468de33f3f73106b5f3861f87821a) on [`student-tts-r2-worker`](https://github.com/akshat6745/AudioBookPython/tree/student-tts-r2-worker). The tagger-manifest fix is [`f6f8c9a4586bd6d300d167a361d1143a0b318a39`](https://github.com/akshat6745/AudioBookPython/commit/f6f8c9a4586bd6d300d167a361d1143a0b318a39). No pull request was opened.

Pushing this branch does not start the Modal workflows. `student-tts-modal.yml` runs on a push to `student-tts-modal` that changes `student_tts_request.json`. `audiobook-render.yml` runs on a push to `audiobook-gpu-render` that changes `render_request.json`.
