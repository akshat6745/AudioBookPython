"""Local worker processes. Credentials come from memory or the environment, never from argv."""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import threading
import traceback

from .exitcodes import EXIT_INTERRUPTED, EXIT_OK, merge_codes
from .redact import redact


def identity_for(run_id: str, worker_index: int, host: str, owner: str) -> dict:
    return {
        "host": host,
        "run_id": run_id,
        "worker_index": worker_index,
        "owner": owner,
        "worker_id": f"{host}/{run_id}/{worker_index}",
    }


def run_threads(workers: list[tuple[dict, object]]) -> int:
    """``workers`` is (identity, callable) pairs. Callables share the in-memory store."""
    errors: queue.Queue = queue.Queue()
    codes: list[int] = []
    lock = threading.Lock()

    def wrap(fn):
        try:
            code = fn()
        except Exception as exc:
            errors.put(exc)
            code = 1
        with lock:
            codes.append(code if code is not None else 1)

    threads = [threading.Thread(target=wrap, args=(fn,), daemon=True) for _identity, fn in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if not errors.empty():
        raise errors.get()
    return merge_codes(*codes) if codes else EXIT_OK


def _process_entry(payload: dict) -> int:
    os.environ["AUDIOBOOK_WORKER_INDEX"] = str(payload["worker_index"])
    namespace = payload.get("namespace") or ""
    if namespace:
        os.environ["AUDIOBOOK_WORKER_NAMESPACE"] = namespace
    try:
        import time

        from .credentials import load_credentials
        from .profile import work_dir
        from .store import BotoStore

        stage = payload["stage"]
        if stage == "tag":
            from .tag_stage import worker_loop
        elif stage == "audio":
            from .audio_stage import worker_loop
        else:
            raise RuntimeError(stage)
        creds = payload.get("credentials")
        if creds is None:
            creds = load_credentials(prompt=False)
        else:
            from .redact import install

            install(creds.secret_values())
        store = BotoStore.connect(creds)
        folder = work_dir(payload["run_id"], payload["worker_index"])
        log_path = folder / "worker.log"

        def log(message: str) -> None:
            line = redact(message)
            print(f"[w{payload['worker_index']}] {line}", flush=True)
            with log_path.open("a") as handle:
                handle.write(line + "\n")

        w_kwargs = dict(
            identity=payload["identity"],
            batch=payload["batch"],
            device=payload["device"],
            now_fn=time.time,
            log=log,
            profile_file=folder / "batch-profile.json",
        )
        if stage == "audio":
            if payload.get("replace_stale"):
                w_kwargs["replace_stale"] = True
            if payload.get("wait_for_tags") is not None:
                w_kwargs["wait_for_tags"] = payload["wait_for_tags"]
            if payload.get("poll_interval") is not None:
                w_kwargs["poll_interval"] = float(payload["poll_interval"])
        return int(worker_loop(
            store,
            payload["source"],
            payload["chapters"],
            **w_kwargs,
        ) or 0)
    except Exception:
        print(redact(traceback.format_exc()), flush=True)
        return 1


def run_processes(stage: str, count: int, payload: dict) -> int:
    ctx = mp.get_context("spawn")
    processes = []
    for index in range(count):
        item = dict(payload)
        item["stage"] = stage
        item["worker_index"] = index
        item["identity"] = identity_for(payload["run_id"], index, payload["host"], item["owners"][index])
        process = ctx.Process(target=_process_entry, args=(item,))
        process.start()
        processes.append(process)
    try:
        codes = []
        for process in processes:
            process.join()
            if process.exitcode is None:
                codes.append(1)
            elif process.exitcode < 0:
                codes.append(EXIT_INTERRUPTED)
            else:
                codes.append(process.exitcode)
        return merge_codes(*codes)
    except KeyboardInterrupt:
        import signal

        for process in processes:
            if process.is_alive() and process.pid:
                os.kill(process.pid, signal.SIGINT)
        for process in processes:
            process.join(timeout=5)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        return EXIT_INTERRUPTED


def run_pipelined_processes(total_workers: int, payload: dict) -> int:
    """Run 1 tagger process alongside (total_workers - 1) audio processes, then spawn the final audio process once tagging finishes."""
    ctx = mp.get_context("spawn")
    tags_done = ctx.Event()
    processes: list[mp.Process] = []

    tag_item = dict(payload)
    tag_item["stage"] = "tag"
    tag_item["batch"] = payload["tag_batch"]
    tag_item["worker_index"] = 0
    tag_item["identity"] = identity_for(payload["run_id"], 0, payload["host"], payload["owners"][0])
    tag_proc = ctx.Process(target=_process_entry, args=(tag_item,))
    tag_proc.start()
    processes.append(tag_proc)

    for index in range(1, total_workers):
        audio_item = dict(payload)
        audio_item["stage"] = "audio"
        audio_item["batch"] = payload["audio_batch"]
        audio_item["worker_index"] = index
        audio_item["identity"] = identity_for(payload["run_id"], index, payload["host"], payload["owners"][index])
        audio_item["wait_for_tags"] = tags_done
        proc = ctx.Process(target=_process_entry, args=(audio_item,))
        proc.start()
        processes.append(proc)

    extra_audio_proc: mp.Process | None = None
    try:
        tag_proc.join()
        tags_done.set()
        if tag_proc.exitcode == 0:
            extra_item = dict(payload)
            extra_item["stage"] = "audio"
            extra_item["batch"] = payload["audio_batch"]
            extra_item["worker_index"] = 0
            extra_item["identity"] = identity_for(payload["run_id"], 0, payload["host"], payload["owners"][0])
            extra_audio_proc = ctx.Process(target=_process_entry, args=(extra_item,))
            extra_audio_proc.start()
            processes.append(extra_audio_proc)

        codes = []
        for process in processes:
            process.join()
            if process.exitcode is None:
                codes.append(1)
            elif process.exitcode < 0:
                codes.append(EXIT_INTERRUPTED)
            else:
                codes.append(process.exitcode)
        return merge_codes(*codes)
    except KeyboardInterrupt:
        import signal

        tags_done.set()
        for process in processes:
            if process.is_alive() and process.pid:
                os.kill(process.pid, signal.SIGINT)
        for process in processes:
            process.join(timeout=5)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        return EXIT_INTERRUPTED
