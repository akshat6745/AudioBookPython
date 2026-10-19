# Two-voice Higgs → StyleTTS2 Modal pipeline

This pipeline is intentionally disarmed. Importing `modal_student_tts.py`, pushing
this branch, or editing the existing `render_request.json` cannot launch it. It
has no GitHub Actions trigger. The local entrypoint exits unless the explicit
spend confirmation flag is present.

## Dataset plan

The deterministic selector spans all five ensemble-tagged novels. It requests
2,700 chunks per voice (5,400 total), designed around an 8-second average:

| Emotion | Narrator | Dialogue | Total |
|---|---:|---:|---:|
| neutral | 900 | 900 | 1,800 |
| anger | 200 | 200 | 400 |
| fear | 200 | 200 | 400 |
| sadness | 200 | 200 | 400 |
| amusement | 200 | 200 | 400 |
| contemplation | 200 | 200 | 400 |
| surprise | 200 | 200 | 400 |
| determination | 200 | 200 | 400 |
| bitterness | 200 | 200 | 400 |
| affection | 200 | 200 | 400 |

Target: about 6 hours/voice and 12 hours total. Neutral is 33.3% by planned
clip count, below the 40–50% ceiling. Requests retain Higgs emotion tags and
are sentence-packed to 1–16 seconds. Final duration is enforced after Whisper:
the QA phase refuses to train if fewer than 10 accepted hours remain.

## GPU policy

- Higgs uses an L40S. The measured safe default is 64 streams
  (about 39.4/48 GB and 14–20× realtime). Startup calibration can test 72 then
  80 on bounded real work. It stops on an error or <5% headroom, never tests
  128, and persists the choice on the Volume.
- Whisper QA is a separate L4 phase, so it cannot contend with Higgs.
- StyleTTS2 calibrates physical batches 8/12/16/24 for at most 12 minutes per
  GPU identity. It chooses examples/second with 7.5% VRAM headroom, persists
  the result separately for L40S/L4, and backs down on OOM.
- CUDA settings: L40S bf16 autocast, TF32, cuDNN benchmark, pinned memory,
  persistent workers, prefetch 4, non-blocking transfers, duration buckets,
  and fused AdamW. `torch.compile` stays off unless a future timed warmup proves
  it improves end-to-end step time.
- Telemetry is JSONL: GPU name, utilization %, VRAM used/total MB, power
  draw/limit W, queue depth, wall seconds, generated/passed audio seconds per
  wall second, examples/second, step time, and peak training VRAM.

The objective is accepted audio/examples per dollar. 100% utilization is not
a goal when it increases QA failures, OOM risk, power, or lowers throughput.
The sustained-utilization target is at least 85% while work is queued. Higgs
is expected to use the measured 39.4 GB at 64 streams. StyleTTS2 VRAM is not
yet measured: calibration limits the selected batch to at most about 44.4/48
GB on L40S or 22.2/24 GB on L4 (7.5% headroom), and raises workers/prefetch
when utilization remains below 85%.

## Cost model (before launch)

Official Modal rates used: L40S $1.9512/h, L4 $0.7992/h, CPU
$0.0131/core-h, memory $0.00222/GiB-h. With 16 CPU cores and 64 GiB, effective
rates are about $2.303/h (L40S) and $1.151/h (L4).

- Higgs: 12 h / 14–20× + 0.25 h setup/calibration = 0.85–1.11 h,
  approximately **$1.96–$2.56**.
- Whisper QA (unmeasured assumption: 8–15×): 12 h / 8–15× + 0.15 h =
  0.95–1.65 h, approximately **$1.09–$1.90**.
- StyleTTS2 L40S (unmeasured assumption: 2–4 s/step): about 5,292 train clips /
  batch 16 × 15 epochs = 4,961 steps; 2.76–5.51 h + 0.2 h calibration =
  2.96–5.71 h, approximately **$6.82–$13.15**.
- StyleTTS2 L4 alternative (unmeasured assumption: batch 8, 3–6 s/step):
  9,930 steps = 8.28–16.55 h + 0.2 h = 8.48–16.75 h,
  approximately **$9.75–$19.27**.

Expected total with L40S training: **$9.87–$17.61**, excluding R2 storage/API
charges and Modal image-build CPU time. L4 training is likely slower and not
necessarily cheaper; calibration decides throughput, not utilization.

## Launch only after approval

```bash
cd /Users/aaggarwal/Desktop/Projects/AudioBookPython-modal-student
modal run modal_student_tts.py::pipeline --gpu l40s --confirm-spend
```

Use `--gpu l4` only to deliberately choose the separately calibrated L4
training path. Do not edit `render_request.json`; it remains unrelated and
disarmed.

## Local checks and artifact download

```bash
PYTHONPATH=scripts/gpu_tts/audiobook:scripts/gpu_tts/student_tts \
  python3 -m pytest -q scripts/gpu_tts/student_tts/test_pipeline.py
python3 -m py_compile modal_student_tts.py scripts/gpu_tts/student_tts/*.py
```

After a successful run:

```bash
python3 scripts/gpu_tts/student_tts/download_and_infer.py \
  --styletts2 scripts/gpu_tts/outputs/student_tts/StyleTTS2
```

This downloads the newest checkpoint/config from
`student_tts/styletts2-eye-v1/` and synthesizes eight sentences per voice.
