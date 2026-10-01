"""Shared FastAPI dependencies.

`get_current_user` is the single place request identity comes from. Endpoints
must take their user from here rather than from a caller-supplied `username`
parameter — that parameter was the whole vulnerability.
"""

from dataclasses import dataclass
from typing import Optional

import structlog
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.tokens import TokenError, decode_access_token

logger = structlog.get_logger()

# auto_error=False so a *missing* Authorization header reaches our code
# instead of FastAPI raising 403 on its own. Both clients treat 401 as
# "session gone, sign in again"; a 403 would slip past those handlers.
_bearer = HTTPBearer(auto_error=False)

_NOT_AUTHENTICATED = "Not authenticated"


@dataclass(frozen=True)
class AuthenticatedUser:
    """Identity taken from a verified token.

    Built from claims without a D1 lookup — D1 is a remote HTTP call and a
    per-request round-trip would tax every endpoint. The token is signed and
    expiring, so the tradeoff is that it stays usable until `exp` even if the
    account is deleted. Endpoints that write a `user_id` foreign key still
    resolve the row from the database rather than trusting `id` here.
    """

    id: str
    username: str


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> AuthenticatedUser:
    """Resolve the caller from their bearer token, or 401."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail=_NOT_AUTHENTICATED)

    try:
        claims = decode_access_token(credentials.credentials)
    except TokenError as exc:
        # Log why it failed; never return the reason, and never the token.
        logger.info(
            "Rejected access token",
            reason=str(exc),
            path=request.url.path,
        )
        raise HTTPException(status_code=401, detail=_NOT_AUTHENTICATED)

    return AuthenticatedUser(id=claims.user_id, username=claims.username)
