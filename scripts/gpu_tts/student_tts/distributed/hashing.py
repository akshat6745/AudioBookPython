"""Streaming hashes."""

from __future__ import annotations

import hashlib
from pathlib import Path


class HashMismatch(Exception):
    def __init__(self, label: str, expected: str, actual: str, expected_size: int | None = None, actual_size: int | None = None):
        self.label = label
        self.expected = expected
        self.actual = actual
        super().__init__(f"hash mismatch for {label}: expected {expected} got {actual}")


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def sha256_iter(chunks) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for block in chunks:
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def verify_bytes(label: str, body: bytes, expected_sha: str, expected_size: int) -> None:
    actual = sha256_bytes(body)
    if actual != expected_sha or len(body) != expected_size:
        raise HashMismatch(label, expected_sha, actual, expected_size, len(body))


def commit_download(partial: Path, dest: Path, expected_sha: str, expected_size: int) -> None:
    actual, size = sha256_file(partial)
    if actual != expected_sha or size != expected_size:
        partial.unlink(missing_ok=True)
        raise HashMismatch(str(dest), expected_sha, actual, expected_size, size)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial.replace(dest)
