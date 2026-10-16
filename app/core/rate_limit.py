"""Small per-instance rate limiter for authenticated, resource-heavy routes.

This bounds accidental or abusive bursts before they reach TTS generation.
Cloud Run instances do not share this memory, so this is a service safeguard,
not a replacement for a shared limiter or Cloud Armor at larger scale.
"""

import asyncio
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Depends, HTTPException

from app.api.deps import AuthenticatedUser, get_current_user


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(
        self,
        *,
        scope: str,
        subject: str,
        limit: int,
        window_seconds: int,
    ) -> None:
        now = time.monotonic()
        cutoff = now - window_seconds
        key = (scope, subject)

        async with self._lock:
            timestamps = self._requests[key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= limit:
                retry_after = max(
                    1, math.ceil(timestamps[0] + window_seconds - now)
                )
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests. Please try again later.",
                    headers={"Retry-After": str(retry_after)},
                )

            timestamps.append(now)

    def clear(self) -> None:
        """Clear state for tests and process-local maintenance."""
        self._requests.clear()


limiter = InMemoryRateLimiter()


def rate_limit(
    scope: str, *, limit: int, window_seconds: int
) -> Callable[..., AuthenticatedUser]:
    """Build a dependency that authenticates and limits a user."""

    async def dependency(
        caller: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        await limiter.check(
            scope=scope,
            subject=caller.id,
            limit=limit,
            window_seconds=window_seconds,
        )
        return caller

    return dependency
