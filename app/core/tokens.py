"""Access-token issuing and verification.

HS256 JWTs signed with JWT_SECRET from the environment. Kept free of D1 and
FastAPI imports so it is directly unit-testable (tests/test_tokens.py).

There is no per-token revocation list, deliberately: checking one would mean a
D1 round-trip on every authenticated request. To invalidate every outstanding
token at once (a leak, or a device you can't reach), rotate JWT_SECRET.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app.core.settings import settings

JWT_ALGORITHM = "HS256"

# A short secret makes the signature brute-forceable offline, which would let
# anyone mint a token for any account.
MIN_SECRET_LENGTH = 32


class TokenError(ValueError):
    """A token could not be issued or trusted."""


@dataclass(frozen=True)
class TokenClaims:
    """The identity a verified token attests to."""

    user_id: str
    username: str


def _require_secret() -> str:
    """The signing secret, or a hard failure.

    Never falls back to a default: a deploy that forgot to set JWT_SECRET must
    refuse to issue tokens rather than sign them with a value an attacker
    could guess from the source.
    """
    secret: Optional[str] = settings.JWT_SECRET
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        raise TokenError("JWT_SECRET is not configured")
    return secret


def create_access_token(user_id: str, username: str) -> str:
    """Issue a signed access token for a user."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + timedelta(days=settings.JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, _require_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> TokenClaims:
    """Verify a token and return its claims.

    Raises TokenError for every failure mode — expired, tampered, signed with
    another secret, malformed, or missing claims — so callers cannot
    accidentally distinguish them in a response.
    """
    try:
        payload = jwt.decode(
            token,
            _require_secret(),
            # Pinned explicitly: without this, a token declaring `alg: none`
            # (or a weaker algorithm) could be accepted unsigned.
            algorithms=[JWT_ALGORITHM],
        )
    except TokenError:
        raise
    except Exception as exc:  # PyJWT raises a family of unrelated types
        raise TokenError("Invalid token") from exc

    user_id = payload.get("sub")
    username = payload.get("username")
    if not user_id or not username:
        raise TokenError("Token is missing required claims")

    return TokenClaims(user_id=str(user_id), username=username)
