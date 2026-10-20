"""Read-only /student-audio allowlist.

The player calls GET /student-audio/<r2-key>. These tests drive the handler
directly, with a fake R2 client, matching the other API tests in this repo.
"""

import asyncio
import inspect

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.student_audio import (
    STUDENT_AUDIO_PREFIX,
    content_type_for_student_audio,
    get_student_audio,
    validate_student_audio_key,
)
from app.main import app

EYE = f"{STUDENT_AUDIO_PREFIX}the-eye-of-the-world"


def _run(coro):
    return asyncio.run(coro)


def _caller() -> AuthenticatedUser:
    return AuthenticatedUser(id="user-1", username="user-1")


def _dependency(function, parameter: str):
    return inspect.signature(function).parameters[parameter].default.dependency


class FakeBody:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0
        self.closed = False

    def read(self, amt=None):
        if amt is None or amt < 0:
            amt = len(self._data) - self._offset
        chunk = self._data[self._offset:self._offset + amt]
        self._offset += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class FakeR2:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.calls: list[tuple] = []

    def get_object(self, Bucket, Key):
        self.calls.append(("get_object", Bucket, Key))
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}},
                "GetObject",
            )
        data = self.objects[Key]
        return {"Body": FakeBody(data), "ContentLength": len(data), "ContentType": "binary/octet-stream"}

    def put_object(self, **kwargs):
        raise AssertionError("student audio endpoint must not write to R2")

    def delete_object(self, **kwargs):
        raise AssertionError("student audio endpoint must not delete from R2")


def _install(monkeypatch, objects: dict[str, bytes]) -> FakeR2:
    fake = FakeR2(objects)
    monkeypatch.setattr("app.api.student_audio._r2_client", lambda: fake)
    monkeypatch.setattr("app.api.student_audio._r2_bucket", lambda: "test-bucket")
    return fake


def _collect(response) -> bytes:
    async def gather():
        parts = []
        async for chunk in response.body_iterator:
            parts.append(chunk)
        return b"".join(parts)

    return _run(gather())


def test_route_is_mounted_and_requires_the_bearer_token():
    paths = [getattr(route, "path", None) for route in app.routes]
    assert "/student-audio/{object_key:path}" in paths
    methods = next(
        route.methods
        for route in app.routes
        if getattr(route, "path", None) == "/student-audio/{object_key:path}"
    )
    assert methods == {"GET"}
    assert _dependency(get_student_audio, "caller") is get_current_user


@pytest.mark.parametrize(
    "filename,media_type,payload",
    [
        ("manifest.json", "application/json", b'{"title":{"file":"title.opus"}}'),
        ("title.opus", "audio/ogg", b"OpusHead-title"),
        ("5.opus", "audio/ogg", b"OpusHead-5"),
        ("chapter.opus", "audio/ogg", b"OpusHead-chapter"),
    ],
)
def test_allowlisted_key_streams_with_player_content_type(monkeypatch, filename, media_type, payload):
    key = f"{EYE}/chapter_1/{filename}"
    fake = _install(monkeypatch, {key: payload})

    response = _run(get_student_audio(key, caller=_caller()))

    assert response.status_code == 200
    assert response.media_type == media_type
    assert content_type_for_student_audio(key) == media_type
    assert response.headers["content-length"] == str(len(payload))
    assert response.headers["accept-ranges"] == "none"
    assert _collect(response) == payload
    assert fake.calls == [("get_object", "test-bucket", key)]


def test_missing_allowlisted_object_is_not_found(monkeypatch):
    fake = _install(monkeypatch, {})
    key = f"{EYE}/chapter_54/title.opus"

    with pytest.raises(HTTPException) as exc:
        _run(get_student_audio(key, caller=_caller()))

    assert exc.value.status_code == 404
    assert fake.calls == [("get_object", "test-bucket", key)]


@pytest.mark.parametrize(
    "key",
    [
        "audio/the-eye-of-the-world/higgs_v3/chapter_1/title.opus",
        "audio/the-eye-of-the-world/chapter_1/manifest.json",
        "audio-student/other-novel/chapter_1/title.opus",
        "audio-student/styletts2-eye-v1/mistborn/chapter_1/title.opus",
        "audio-student/styletts2-eye-v1/the-eye-of-the-world/chapter_1/notes.txt",
        "audio-student/styletts2-eye-v1-evil/the-eye-of-the-world/chapter_1/title.opus",
        f"{EYE}/chapter_0/title.opus",
        f"{EYE}/chapter_1/../chapter_2/title.opus",
        "audio-student/styletts2-eye-v1/../../audio/secret.opus",
        f"{EYE}/chapter_1/../../../audio/production.mp3",
        "../audio/the-eye-of-the-world/chapter_1/title.opus",
        "/audio/the-eye-of-the-world/chapter_1/title.opus",
        f"{EYE}/chapter_1/title.opus\x00.mp3",
        "audio-student\\styletts2-eye-v1\\the-eye-of-the-world\\chapter_1\\title.opus",
        "https://evil.example/audio-student/styletts2-eye-v1/the-eye-of-the-world/chapter_1/title.opus",
        f"{EYE}//chapter_1/title.opus",
        f" {EYE}/chapter_1/title.opus",
    ],
)
def test_rejected_prefixes_and_traversal_never_touch_r2(monkeypatch, key):
    fake = _install(monkeypatch, {key: b"should-not-be-read"})

    with pytest.raises(HTTPException) as exc:
        validate_student_audio_key(key)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as endpoint_exc:
        _run(get_student_audio(key, caller=_caller()))
    assert endpoint_exc.value.status_code == 403
    assert fake.calls == []


def test_r2_failure_does_not_echo_the_client_error(monkeypatch):
    class BrokenR2:
        def get_object(self, Bucket, Key):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "secret-bucket-detail"}},
                "GetObject",
            )

    monkeypatch.setattr("app.api.student_audio._r2_client", lambda: BrokenR2())
    monkeypatch.setattr("app.api.student_audio._r2_bucket", lambda: "test-bucket")

    with pytest.raises(HTTPException) as exc:
        _run(get_student_audio(f"{EYE}/chapter_1/manifest.json", caller=_caller()))

    assert exc.value.status_code == 502
    assert "secret-bucket-detail" not in str(exc.value.detail)
