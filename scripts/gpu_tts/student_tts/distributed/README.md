# Distributed student audiobook worker

Tagging and audio are separate stages. Both read and write Cloudflare R2. A chapter is claimed with a conditional put, so two machines, or two local workers, cannot do the same chapter.

Recommended order:

```bash
./bootstrap.sh
./audiobook-worker publish-novel --name "The Eye of the World" --source-dir /path/to/book --tags-dir /path/to/book/tags --chapters 1-54
./audiobook-worker run --novel "The Eye of the World" --stage tag --workers 4 --batch-size 32 --validate
./audiobook-worker validate --novel "The Eye of the World" --stage tag
./audiobook-worker run --novel "The Eye of the World" --stage audio --workers 2 --batch-size 4 --validate
./audiobook-worker validate --novel "The Eye of the World" --stage audio
```

`./audiobook-worker --novel "The Eye of the World"` is `run` with `--stage all`, which tags missing chapters and then renders chapters whose tags validate. The examples above pass `--stage` so each step is explicit. The same commands work as `python -m scripts.gpu_tts.student_tts.distributed ...` from the repository root.

Do not start `--stage audio` for The Eye of the World while the older local renderer is still uploading those chapters. That older process does not take these leases.

## Stages

`--stage tag` writes tag manifests for chapters that are missing or stale. It does not download or load StyleTTS and it does not write audio.

`--stage audio` (alias `--stage render`) writes audio only for chapters whose R2 tag manifest validates. It does not run the tagger. If any requested chapter lacks a valid tag manifest, it stops before loading StyleTTS and prints the incomplete chapters. `--allow-partial-tags` renders the chapters that already have valid tags; the default is to refuse.

`--stage all` runs tag, then audio. Validation with `--stage all` checks source, tags, and audio.

`validate` does not generate anything and does not load model weights. `run --validate` does the stage, then the same validation, and exits non-zero if the gate fails.

`--report-json PATH` writes a summary. Credential values are replaced before the file is written.

Exit codes: `0` complete, `2` missing or still leased or mid-publish, `3` invalid, stale, hash mismatch, or conflict, `4` setup, credentials, unregistered novel, or a chapter outside the source manifest, `130` interrupted. If a run contains both missing and invalid chapters, the exit is `3`.

## Workers and batch size

`--workers N` is the number of local worker processes for the selected stage. The default is 1. It is not the model batch size. Each worker has the identity `host/run-id/index`, its own temp directory, and its own log. They share the R2 lease. Validation `--workers` only parallelizes HEAD, GET, and hash checks and does not take a lease.

A GPU stage usually does not go faster with several workers on one GPU, because each worker loads the model. The tagger can use more than one worker when memory allows.

`--batch-size N` is the model batch for the selected stage. For tagging it is the number of paragraphs in one DeBERTa forward pass. For audio it is the maximum number of TTS chunks in one forward pass, still limited by the 1600 token budget. If you omit it, tagging uses 32 and audio uses 4 (1 on CPU). `--tag-batch-size` and `--audio-batch-size` override that stage when both stages run together. An explicit value is not silently raised. On out-of-memory the worker retries at floor(N/2) down to 1 and stores the successful size on this machine, keyed by stage, device, and model version.

## Chapter ranges

`--chapters 1-54` and `--chapters 1,2,5-7` select the same chapters for `run` and `validate`. The source manifest is the authority. A chapter that is not in it exits `4`. The scan is the whole selection: valid 1–18, missing 19, valid 20 processes only 19.

## Manifests

A tag manifest records the source chapter hash, tagger runtime and model version, schema version, paragraph count, JSONL size and SHA256, the effective batch, and the worker id. A mismatch of source hash, model, or schema is stale. `--stage tag` regenerates it under the lease. The JSONL object is content-addressed and is not replaced with different bytes. The small manifest is written last.

An audio manifest records the source hash, tag manifest hash, StyleTTS runtime, checkpoint, and style-bank versions, plus the title, paragraph, and chapter object keys with sizes and SHA256. A dependency mismatch is stale. New bytes go to a unique attempt prefix, then `publish.json` is elected with If-None-Match, then the final objects are copied with If-None-Match, then the manifest is written. Existing canonical audio that already matches its sizes and hashes is left as legacy-valid, including Eye chapters already published by the older renderer. A stale `student-audio/v1` manifest is rewritten under `audio-student/styletts2-eye-v1/<slug>/runtimes/<runtime>/chapter_<n>/` instead of overwriting the canonical keys.

Validation statuses are `valid`, `legacy_valid`, `missing`, `leased`, `publishing`, `stale`, `hash_mismatch`, `conflict`, and `invalid`.

## Platforms and credentials

Python 3.11. Device order is CUDA, then Apple Silicon MPS, then CPU. Override with `--device cuda|mps|cpu`. Windows runs through Ubuntu on WSL2 with `ffmpeg`, `libopus`, and `espeak-ng`. `./bootstrap.sh` prints the brew or apt line when a tool is missing, creates `.venv-r2worker` from public PyPI (`PIP_CONFIG_FILE=/dev/null`), clones StyleTTS2 at `5cedc71c333f8d8b8551ca59378bdcc7af4c9529`, applies the inference patch, and downloads the versioned artifacts.

R2 needs an endpoint, bucket, access key id, and secret. A Cloudflare API token cannot sign these requests. Create a bucket-scoped access key. Values come from the environment, then `./.env`, `$AUDIOBOOK_R2_ENV`, or `~/.config/audiobook-worker/r2.env`, then a hidden prompt. There is no secret flag. `.env` is gitignored.

## Coordination

The lease is `student_tts/coord/<slug>/<stage>/chapter_NNNNN/lease.json`. Claim is `PutObject` with `If-None-Match: *`. Heartbeats every 60 seconds and stale takeover after 20 minutes use `If-Match`. A timed-out write is accepted only after `HeadObject` shows this worker still owns it. Ctrl-C marks the lease `released` with `If-Match` when this process still owns it. The lease object is not deleted. Completion is a validated R2 manifest, not a local file.

## Register and publish

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

The source directory for `publish-novel` contains `chapters/00001.txt` and `titles.json`. Chapter 55 of The Eye of the World is the preview and is not part of the 1–54 contract. `publish-runtime` checks audited hashes, keeps the existing checkpoint key when size and ETag match, and uploads missing auxiliary files and the tagger bundle. It does not upload a virtualenv, Qwen, or the training set, and it refuses to run while `AUDIOBOOK_WORKER_NAMESPACE` is set. It does not write `audio-student/` keys.

Style-bank emotions are neutral, affection, amusement, anger, bitterness, contemplation, determination, fear, sadness, and surprise. Any other label uses that voice's neutral vectors. Audio is 24 kHz mono, style mix 0.7/0.3, diffusion 5, embedding scale 1, Opus 64 kbps, loudness -18 LUFS, limiter 0.84.
