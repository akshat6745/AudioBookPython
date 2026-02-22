"""
Singleton D1 client for FastAPI.
Uses the Cloudflare D1 REST API via httpx.AsyncClient.
"""

import httpx
from typing import Optional
from app.core.settings import settings


class D1Client:
    """Async wrapper around the Cloudflare D1 REST API."""

    def __init__(self):
        self._http = httpx.AsyncClient(timeout=15)
        self._url = (
            f"https://api.cloudflare.com/client/v4/accounts/{settings.CF_ACCOUNT_ID}"
            f"/d1/database/{settings.D1_DATABASE_ID}/query"
        )
        self._headers = {
            "Authorization": f"Bearer {settings.CF_API_TOKEN}",
            "Content-Type": "application/json",
        }

    async def query(self, sql: str, params: list = None) -> list[dict]:
        """Run a SELECT — returns list of row dicts."""
        resp = await self._http.post(
            self._url,
            headers=self._headers,
            json={"sql": sql, "params": params or []},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"D1 error: {data.get('errors')}")
        return data["result"][0].get("results", [])

    async def execute(self, sql: str, params: list = None) -> dict:
        """Run INSERT / UPDATE / DELETE — returns meta dict."""
        resp = await self._http.post(
            self._url,
            headers=self._headers,
            json={"sql": sql, "params": params or []},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"D1 error: {data.get('errors')}")
        return data["result"][0].get("meta", {})

    async def close(self):
        await self._http.aclose()


# ── Singleton ─────────────────────────────────────────────────────────────────
_d1: Optional[D1Client] = None


def get_d1_client() -> D1Client:
    global _d1
    if _d1 is None:
        _d1 = D1Client()
    return _d1
