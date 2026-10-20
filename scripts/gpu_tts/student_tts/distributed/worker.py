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

        return int(worker_loop(
            store,
            payload["source"],
            payload["chapters"],
            identity=payload["identity"],
            batch=payload["batch"],
            device=payload["device"],
            now_fn=time.time,
            log=log,
            profile_file=folder / "batch-profile.json",
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
