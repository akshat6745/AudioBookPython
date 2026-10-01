"""Unit tests for app/core/tokens.py — pure, no D1 or network.

Run: python -m pytest tests/test_tokens.py
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.settings import settings
from app.core.tokens import (
    JWT_ALGORITHM,
    TokenError,
    create_access_token,
    decode_access_token,
)

SECRET = "test-secret-that-is-long-enough-to-be-accepted"
OTHER_SECRET = "a-different-secret-also-long-enough-to-pass"


@pytest.fixture(autouse=True)
def _configured_secret(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET", SECRET, raising=False)
    monkeypatch.setattr(settings, "JWT_EXPIRY_DAYS", 30, raising=False)


def test_roundtrip_preserves_identity():
    claims = decode_access_token(create_access_token("user-1", "akshat"))
    assert claims.user_id == "user-1"
    assert claims.username == "akshat"


def test_token_does_not_contain_the_secret():
    token = create_access_token("user-1", "akshat")
    assert SECRET not in token


def test_expired_token_is_rejected():
    expired = jwt.encode(
        {
            "sub": "user-1",
            "username": "akshat",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        SECRET,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(TokenError):
        decode_access_token(expired)


def test_token_signed_with_another_secret_is_rejected():
    forged = jwt.encode(
        {
            "sub": "user-1",
            "username": "akshat",
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        OTHER_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(TokenError):
        decode_access_token(forged)


def test_tampered_payload_is_rejected():
    """Flipping a claim invalidates the signature."""
    header, payload, signature = create_access_token("user-1", "akshat").split(".")
    other = create_access_token("user-2", "victim").split(".")[1]
    with pytest.raises(TokenError):
        decode_access_token(f"{header}.{other}.{signature}")


def test_unsigned_alg_none_token_is_rejected():
    """The classic JWT bypass: an unsigned token claiming alg=none."""
    unsigned = jwt.encode(
        {
            "sub": "user-1",
            "username": "victim",
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(TokenError):
        decode_access_token(unsigned)


def test_malformed_token_is_rejected():
    for garbage in ("", "not-a-token", "a.b.c", "Bearer xyz"):
        with pytest.raises(TokenError):
            decode_access_token(garbage)


def test_token_without_required_claims_is_rejected():
    no_username = jwt.encode(
        {"sub": "user-1", "exp": datetime.now(timezone.utc) + timedelta(days=1)},
        SECRET,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(TokenError):
        decode_access_token(no_username)


class TestSecretConfiguration:
    """A misconfigured deploy must refuse to issue or trust tokens."""

    def test_missing_secret_cannot_issue(self, monkeypatch):
        monkeypatch.setattr(settings, "JWT_SECRET", None, raising=False)
        with pytest.raises(TokenError):
            create_access_token("user-1", "akshat")

    def test_missing_secret_cannot_verify(self, monkeypatch):
        token = create_access_token("user-1", "akshat")
        monkeypatch.setattr(settings, "JWT_SECRET", None, raising=False)
        with pytest.raises(TokenError):
            decode_access_token(token)

    def test_short_secret_is_refused(self, monkeypatch):
        """A brute-forceable secret is as good as no signature at all."""
        monkeypatch.setattr(settings, "JWT_SECRET", "tooshort", raising=False)
        with pytest.raises(TokenError):
            create_access_token("user-1", "akshat")
