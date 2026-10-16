"""Security regression tests for TTS and server-side downloads."""

import asyncio
import inspect

import pytest
from fastapi import HTTPException

from app.api import download, download_v2, novels, tts
from app.api.deps import AuthenticatedUser, get_current_user
from app.core.rate_limit import InMemoryRateLimiter


def _run(coro):
    return asyncio.run(coro)


def _caller(user_id: str = "user-1") -> AuthenticatedUser:
    return AuthenticatedUser(id=user_id, username=user_id)


def _dependency(function, parameter: str):
    return inspect.signature(function).parameters[parameter].default.dependency


def test_tts_routes_require_authentication_and_generation_is_rate_limited():
    assert _dependency(tts.list_tts_voices, "caller") is get_current_user
    assert _dependency(tts.text_to_speech_dual_voice_post, "caller") is tts._tts_rate_limit
    assert _dependency(novels.novel_with_tts, "caller") is novels._tts_rate_limit


def test_download_routes_require_authentication_and_starts_are_rate_limited():
    assert _dependency(download.get_download_progress, "caller") is get_current_user
    assert _dependency(download.download_chapter, "caller") is download._download_rate_limit
    assert (
        _dependency(download_v2.start_chapter_download, "caller")
        is download_v2._download_rate_limit
    )
    assert _dependency(download_v2.get_download_status, "caller") is get_current_user
    assert _dependency(download_v2.download_file, "caller") is get_current_user


def test_rate_limit_is_scoped_per_user_and_operation(monkeypatch):
    current_time = 100.0
    monkeypatch.setattr("app.core.rate_limit.time.monotonic", lambda: current_time)
    limiter = InMemoryRateLimiter()

    _run(limiter.check(scope="tts", subject="user-1", limit=2, window_seconds=60))
    _run(limiter.check(scope="tts", subject="user-1", limit=2, window_seconds=60))

    with pytest.raises(HTTPException) as exc:
        _run(limiter.check(scope="tts", subject="user-1", limit=2, window_seconds=60))
    assert exc.value.status_code == 429
    assert exc.value.headers == {"Retry-After": "60"}

    # A different user and a different operation have independent budgets.
    _run(limiter.check(scope="tts", subject="user-2", limit=2, window_seconds=60))
    _run(limiter.check(scope="download", subject="user-1", limit=2, window_seconds=60))


def test_download_metadata_is_visible_only_to_its_owner():
    download_v2.download_status["owned-id"] = {
        "_owner_id": "owner",
        "status": "pending",
    }
    try:
        assert download_v2._owned_download("owned-id", _caller("owner"))["status"] == "pending"
        with pytest.raises(HTTPException) as exc:
            download_v2._owned_download("owned-id", _caller("other"))
        assert exc.value.status_code == 404
    finally:
        download_v2.download_status.pop("owned-id", None)
