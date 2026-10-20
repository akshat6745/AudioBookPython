"""Global chapter leases on R2 conditional puts.

A lease is claimed with PutObject If-None-Match ``*``. Heartbeats and stale takeover
use If-Match on the current ETag. A lease is never deleted to unlock a chapter.
Ctrl-C marks the lease ``released`` with If-Match when this process still owns it;
otherwise the lease expires after the TTL (20 minutes).
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass

from .layout import HEARTBEAT_S, LEASE_TTL_S, lease_key
from .store import PreconditionFailed, RequestTimeout

ACTIVE = "active"
RELEASED = "released"


def dumps(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def loads(body: bytes) -> dict:
    return json.loads(body.decode())


@dataclass
class Claim:
    store: object
    key: str
    etag: str
    lease: dict

    def heartbeat(self, now: float) -> bool:
        updated = dict(self.lease)
        updated["updated_unix"] = now
        updated["updated"] = _iso(now)
        body = dumps(updated)
        try:
            self.etag = self.store.put_bytes(self.key, body, content_type="application/json", if_match=self.etag)
        except RequestTimeout:
            return self._adopt(updated["owner"], updated["run_id"], int(updated["worker_index"]))
        except PreconditionFailed:
            return False
        self.lease = updated
        return True

    def release(self, now: float) -> bool:
        """CAS the lease to released. Failure leaves the object in place until TTL."""
        updated = dict(self.lease)
        updated["status"] = RELEASED
        updated["updated_unix"] = now
        updated["updated"] = _iso(now)
        body = dumps(updated)
        try:
            self.etag = self.store.put_bytes(self.key, body, content_type="application/json", if_match=self.etag)
        except (PreconditionFailed, RequestTimeout):
            adopted = self._adopt(updated["owner"], updated["run_id"], int(updated["worker_index"]))
            if not adopted:
                return False
            try:
                self.etag = self.store.put_bytes(self.key, body, content_type="application/json", if_match=self.etag)
            except (PreconditionFailed, RequestTimeout):
                return False
        self.lease = updated
        return True

    def _adopt(self, owner: str, run_id: str, worker_index: int) -> bool:
        head = self.store.head(self.key)
        if head is None:
            return False
        try:
            current = loads(self.store.get_bytes(self.key))
        except Exception:
            return False
        if current.get("owner") == owner and current.get("run_id") == run_id and int(current.get("worker_index", -1)) == worker_index:
            self.etag = head.etag
            self.lease = current
            return True
        return False


def _iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_lease(*, owner: str, host: str, stage: str, chapter: int, now: float, runtime_version: str, run_id: str, worker_index: int, pid: int) -> dict:
    return {
        "owner": owner,
        "host": host,
        "stage": stage,
        "chapter": chapter,
        "started": _iso(now),
        "started_unix": now,
        "updated": _iso(now),
        "updated_unix": now,
        "runtime_version": runtime_version,
        "run_id": run_id,
        "worker_index": worker_index,
        "pid": pid,
        "status": ACTIVE,
    }


def is_stale(lease: dict, now: float, ttl: float = LEASE_TTL_S) -> bool:
    if lease.get("status") != ACTIVE:
        return True
    updated = float(lease.get("updated_unix", 0))
    return now - updated >= ttl


def read_lease(store, key: str) -> tuple[dict, str] | None:
    head = store.head(key)
    if head is None:
        return None
    return loads(store.get_bytes(key)), head.etag


def claim_chapter(
    store,
    *,
    slug: str,
    stage: str,
    chapter: int,
    owner: str,
    run_id: str,
    worker_index: int,
    runtime_version: str,
    now: float,
    host: str | None = None,
    pid: int = 0,
    ttl: float = LEASE_TTL_S,
) -> Claim | None:
    """Return a claim, or None when another live worker holds the chapter."""
    key = lease_key(slug, stage, chapter)
    host = host or socket.gethostname()
    lease = make_lease(
        owner=owner,
        host=host,
        stage=stage,
        chapter=chapter,
        now=now,
        runtime_version=runtime_version,
        run_id=run_id,
        worker_index=worker_index,
        pid=pid,
    )
    body = dumps(lease)
    try:
        etag = store.put_bytes(key, body, content_type="application/json", if_none_match="*")
        return Claim(store, key, etag, lease)
    except RequestTimeout:
        adopted = _adopt_new(store, key, owner, run_id, worker_index)
        if adopted:
            return adopted
        return None
    except PreconditionFailed:
        return _takeover(
            store,
            key,
            lease,
            owner=owner,
            run_id=run_id,
            worker_index=worker_index,
            now=now,
            ttl=ttl,
        )


def _adopt_new(store, key: str, owner: str, run_id: str, worker_index: int) -> Claim | None:
    found = read_lease(store, key)
    if found is None:
        return None
    current, etag = found
    if current.get("owner") == owner and current.get("run_id") == run_id and int(current.get("worker_index", -1)) == worker_index:
        return Claim(store, key, etag, current)
    return None


def _takeover(store, key, lease, *, owner, run_id, worker_index, now, ttl) -> Claim | None:
    found = read_lease(store, key)
    if found is None:
        try:
            etag = store.put_bytes(key, dumps(lease), content_type="application/json", if_none_match="*")
        except (PreconditionFailed, RequestTimeout):
            return _adopt_new(store, key, owner, run_id, worker_index)
        return Claim(store, key, etag, lease)
    current, etag = found
    if current.get("owner") == owner and current.get("run_id") == run_id and int(current.get("worker_index", -1)) == worker_index:
        return Claim(store, key, etag, current)
    if not is_stale(current, now, ttl):
        return None
    body = dumps(lease)
    try:
        new_etag = store.put_bytes(key, body, content_type="application/json", if_match=etag)
    except PreconditionFailed:
        return None
    except RequestTimeout:
        return _adopt_new(store, key, owner, run_id, worker_index)
    return Claim(store, key, new_etag, lease)


def lease_view(store, slug: str, stage: str, chapter: int, now: float, ttl: float = LEASE_TTL_S) -> dict | None:
    found = read_lease(store, lease_key(slug, stage, chapter))
    if found is None:
        return None
    lease, _etag = found
    lease = dict(lease)
    lease["stale"] = is_stale(lease, now, ttl)
    return lease


HEARTBEAT_INTERVAL_S = HEARTBEAT_S


class Heartbeat:
    """Refresh the lease while a chapter runs. Stop does not delete the object."""

    def __init__(self, claim: Claim, now_fn, interval: float = HEARTBEAT_S):
        import threading

        self.claim = claim
        self.now_fn = now_fn
        self.interval = interval
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if not self.claim.heartbeat(self.now_fn()):
                self.lost = True
                return

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_args) -> bool:
        self._stop.set()
        self._thread.join(timeout=1)
        return False
