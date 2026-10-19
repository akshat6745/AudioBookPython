"""Idempotently add CUDA throughput controls and calibration hooks to upstream StyleTTS2 (commit 5cedc71).

Environment read by the patched train_finetune.py:
  MAX_STEPS        stop after n steps and print TRAIN_METRICS (0 = full run)
  STYLETTS_AMP     bf16 | fp16 | off   (global autocast; calibration falls back to off if it crashes)
  TRAIN_WORKERS / TRAIN_PREFETCH       DataLoader workers and prefetch factor
"""
from __future__ import annotations

from pathlib import Path

MARK = "# student_tts cuda patch"


def replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        raise RuntimeError(f"upstream StyleTTS2 changed; missing patch anchor: {old[:80]!r}")
    return text.replace(old, new, 1)


def patch(root: Path = Path("/opt/styletts2")) -> None:
    train = root / "train_finetune.py"
    text = train.read_text()
    if MARK not in text:
        text = replace_once(text, "import random\n", f"import random\nimport json\nimport os\n{MARK}\n")
        text = replace_once(
            text, "    device = 'cuda'\n",
            "    device = 'cuda'\n"
            "    torch.backends.cuda.matmul.allow_tf32 = True\n"
            "    torch.backends.cudnn.allow_tf32 = True\n"
            "    torch.backends.cudnn.benchmark = True\n"
            "    torch.set_float32_matmul_precision('high')\n"
            "    amp = os.environ.get('STYLETTS_AMP', 'off')\n"
            "    if amp in ('bf16', 'fp16'):\n"
            "        torch.set_autocast_enabled('cuda', True)\n"
            "        torch.set_autocast_dtype('cuda', torch.bfloat16 if amp == 'bf16' else torch.float16)\n"
            "    print('CUDA_SETTINGS ' + json.dumps({'amp': amp, 'tf32': True, 'cudnn_benchmark': True, "
            "'gpu': torch.cuda.get_device_name(0)}), flush=True)\n"
            "    max_steps = int(os.environ.get('MAX_STEPS', '0'))\n"
            "    slm_steps = 0  # steps where slmadv returned a loss; upstream skips it silently otherwise\n"
            "    warm_t = None\n"
            "    train_workers = int(os.environ.get('TRAIN_WORKERS', '8'))\n",
        )
        text = replace_once(text, "                                        num_workers=2,",
                            "                                        num_workers=train_workers,")
        text = replace_once(text, "            batch = [b.to(device) for b in batch[1:]]",
                            "            batch = [b.to(device, non_blocking=True) for b in batch[1:]]")
        text = replace_once(
            text, "            iters = iters + 1\n",
            "            iters = iters + 1\n"
            "            if iters == 5:\n"
            "                torch.cuda.synchronize()\n"
            "                warm_t = time.perf_counter()\n"
            "            if max_steps and iters >= max_steps:\n"
            "                torch.cuda.synchronize()\n"
            "                elapsed = time.perf_counter() - warm_t\n"
            "                steps = iters - 5\n"
            "                print('TRAIN_METRICS ' + json.dumps({'steps': steps, 'wall_s': elapsed, "
            "'examples_per_s': steps * batch_size / elapsed, 'step_s': elapsed / steps, "
            "'peak_vram_gb': torch.cuda.max_memory_reserved() / 2**30, 'slm_steps': slm_steps}), flush=True)\n"
            "                return\n"
            "            if iters % 100 == 0:\n"
            "                print('TRAIN_PROGRESS ' + json.dumps({'epoch': epoch + 1, 'iters': iters, "
            "'slm_steps': slm_steps, 'peak_vram_gb': round(torch.cuda.max_memory_reserved() / 2**30, 2)}), "
            "flush=True)\n",
        )
        text = replace_once(text, "                if slm_out is not None:\n",
                            "                if slm_out is not None:\n"
                            "                    slm_steps += 1\n")
        text = replace_once(text, "        print('Epochs:', epoch + 1)\n",
                            "        print('Epochs:', epoch + 1)\n"
                            "        print('SLM_STEPS ' + json.dumps({'epoch': epoch + 1, 'slm_steps': slm_steps, "
                            "'iters': iters}), flush=True)\n")
        text = replace_once(
            text,
            "        model, optimizer, start_epoch, iters = load_checkpoint(model,  optimizer, config['pretrained_model'],\n"
            "                                    load_only_params=config.get('load_only_params', True))\n",
            "        model, optimizer, start_epoch, iters = load_checkpoint(model,  optimizer, config['pretrained_model'],\n"
            "                                    load_only_params=config.get('load_only_params', True))\n"
            "        if not config.get('load_only_params', True):\n"
            "            start_epoch += 1\n",
        )
        text = replace_once(text, "            torch.save(state, save_path)",
                            "            torch.save(state, save_path + '.tmp')\n"
                            "            os.replace(save_path + '.tmp', save_path)")
        train.write_text(text)

    loader = root / "meldataset.py"
    text = loader.read_text()
    if "persistent_workers" not in text:
        text = replace_once(
            text, "                             pin_memory=(device != 'cpu'))",
            "                             pin_memory=(device != 'cpu'),\n"
            "                             persistent_workers=(num_workers > 0),\n"
            "                             prefetch_factor=(int(__import__('os').environ.get('TRAIN_PREFETCH', '4')) "
            "if num_workers > 0 else None))",
        )
        loader.write_text(text)

    optim = root / "optimizers.py"
    text = optim.read_text()
    if "fused=" not in text:
        text = replace_once(
            text, "AdamW(params, lr=lr, weight_decay=1e-4, betas=(0.0, 0.99), eps=1e-9)",
            "AdamW(params, lr=lr, weight_decay=1e-4, betas=(0.0, 0.99), eps=1e-9, "
            "fused=torch.cuda.is_available())",
        )
        optim.write_text(text)


if __name__ == "__main__":
    patch(Path(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else Path("/opt/styletts2"))
