"""Object storage. Leases use conditional puts. ``FakeStore`` is the in-memory R2 used by tests."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from .hashing import commit_download, sha256_file


class PreconditionFailed(Exception):
    pass


class RequestTimeout(Exception):
    pass


class NotFound(Exception):
    pass


class ConflictError(Exception):
    pass


@dataclass
class Head:
    key: str
    size: int
    etag: str
    metadata: dict = field(default_factory=dict)


def _etag_unquote(etag: str) -> str:
    if etag.startswith('"') and etag.endswith('"'):
        return etag[1:-1]
    return etag


class FakeStore:
    """Atomic conditional put/head/get. Optional timeout-after-apply simulates a lost response."""

    def __init__(self):
        self._lock = threading.Lock()
        self.objects: dict[str, dict] = {}
        self._seq = 0
        self.timeout_mode: str | None = None
        self.delete_calls: list[str] = []
        self.puts: list[str] = []

    def put_bytes(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_none_match: str | None = None,
        if_match: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("put_bytes expects bytes")
        with self._lock:
            if self.timeout_mode == "before":
                self.timeout_mode = None
                raise RequestTimeout("timeout before apply")
            existing = self.objects.get(key)
            if if_none_match == "*" and existing is not None:
                raise PreconditionFailed(key)
            if if_match is not None:
                if existing is None or existing["etag"] != if_match:
                    raise PreconditionFailed(key)
            self._seq += 1
            etag = f'"{self._seq:032x}"'
            self.objects[key] = {
                "body": bytes(body),
                "etag": etag,
                "content_type": content_type,
                "metadata": dict(metadata or {}),
            }
            self.puts.append(key)
            if self.timeout_mode == "after":
                self.timeout_mode = None
                raise RequestTimeout("timeout after apply")
            return etag

    def head(self, key: str) -> Head | None:
        with self._lock:
            existing = self.objects.get(key)
            if existing is None:
                return None
            return Head(key, len(existing["body"]), existing["etag"], dict(existing["metadata"]))

    def get_bytes(self, key: str) -> bytes:
        with self._lock:
            existing = self.objects.get(key)
            if existing is None:
                raise NotFound(key)
            return bytes(existing["body"])

    def list(self, prefix: str) -> list[Head]:
        with self._lock:
            heads = []
            for key, existing in sorted(self.objects.items()):
                if key.startswith(prefix):
                    heads.append(Head(key, len(existing["body"]), existing["etag"], dict(existing["metadata"])))
            return heads

    def delete(self, key: str) -> None:
        """Present so tests can prove leases never call it. Not part of the lock protocol."""
        with self._lock:
            self.delete_calls.append(key)
            self.objects.pop(key, None)

    def download(self, key: str, dest: Path, expected_sha: str, expected_size: int) -> Path:
        body = self.get_bytes(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".partial")
        partial.write_bytes(body)
        commit_download(partial, dest, expected_sha, expected_size)
        return dest


def _status(error) -> tuple[str, int]:
    code = error.response.get("Error", {}).get("Code", "")
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return str(code), int(status or 0)


def _map_client_error(error):
    from botocore.exceptions import ClientError

    if not isinstance(error, ClientError):
        return error
    code, status = _status(error)
    if code in {"PreconditionFailed", "412"} or status == 412:
        return PreconditionFailed(code or "412")
    if code in {"NoSuchKey", "404", "NotFound"} or status == 404:
        return NotFound(code or "404")
    return error


class BotoStore:
    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def connect(cls, creds) -> "BotoStore":
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=creds.endpoint_url,
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=60,
                read_timeout=3600,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return cls(client, creds.bucket)

    def put_bytes(self, key, body, *, content_type="application/octet-stream", if_none_match=None, if_match=None, metadata=None) -> str:
        return self._put(key, body, len(body), content_type, if_none_match, if_match, metadata)

    def put_file(self, key, path: Path, *, content_type="application/octet-stream", if_none_match=None, if_match=None, metadata=None) -> str:
        size = path.stat().st_size
        with path.open("rb") as handle:
            return self._put(key, handle, size, content_type, if_none_match, if_match, metadata)

    def _put(self, key, body, size, content_type, if_none_match, if_match, metadata) -> str:
        from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

        kwargs = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            "ContentLength": size,
            "ContentType": content_type,
        }
        if if_none_match:
            kwargs["IfNoneMatch"] = if_none_match
        if if_match:
            kwargs["IfMatch"] = if_match
        if metadata:
            kwargs["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
        try:
            response = self.client.put_object(**kwargs)
        except (ReadTimeoutError, ConnectTimeoutError) as exc:
            raise RequestTimeout(type(exc).__name__) from exc
        except ClientError as exc:
            mapped = _map_client_error(exc)
            if mapped is not exc:
                raise mapped from exc
            raise
        return response["ETag"]

    def head(self, key: str) -> Head | None:
        from botocore.exceptions import ClientError

        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            mapped = _map_client_error(exc)
            if isinstance(mapped, NotFound):
                return None
            if mapped is not exc:
                raise mapped from exc
            raise
        return Head(key, int(response["ContentLength"]), response["ETag"], dict(response.get("Metadata") or {}))

    def get_bytes(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            mapped = _map_client_error(exc)
            if mapped is not exc:
                raise mapped from exc
            raise
        return response["Body"].read()

    def iter_body(self, key: str):
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            mapped = _map_client_error(exc)
            if mapped is not exc:
                raise mapped from exc
            raise
        body = response["Body"]
        while True:
            chunk = body.read(8 * 1024 * 1024)
            if not chunk:
                break
            yield chunk

    def list(self, prefix: str) -> list[Head]:
        heads: list[Head] = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for item in page.get("Contents") or []:
                heads.append(Head(item["Key"], int(item["Size"]), item.get("ETag", ""), {}))
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
        return heads

    def download(self, key: str, dest: Path, expected_sha: str, expected_size: int) -> Path:
        from botocore.exceptions import ClientError

        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            actual, size = sha256_file(dest)
            if actual == expected_sha and size == expected_size:
                return dest
            dest.unlink()
        partial = dest.with_suffix(dest.suffix + ".partial")
        have = partial.stat().st_size if partial.exists() else 0
        if have > expected_size:
            partial.unlink()
            have = 0
        kwargs = {"Bucket": self.bucket, "Key": key}
        if have:
            kwargs["Range"] = f"bytes={have}-"
        try:
            response = self.client.get_object(**kwargs)
        except ClientError as exc:
            mapped = _map_client_error(exc)
            if mapped is not exc:
                raise mapped from exc
            raise
        mode = "ab" if have else "wb"
        with partial.open(mode) as handle:
            body = response["Body"]
            while True:
                chunk = body.read(8 * 1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        commit_download(partial, dest, expected_sha, expected_size)
        return dest

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def etag_equal(left: str, right: str) -> bool:
    return _etag_unquote(left) == _etag_unquote(right)
