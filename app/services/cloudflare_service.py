"""
Cloudflare D1 + R2 service layer for AudioBookPython.

D1 is accessed via REST API (httpx).
R2 is accessed via boto3 (S3-compatible).
"""

import os
import gzip
from functools import lru_cache

import boto3
import httpx
from botocore.exceptions import ClientError
from app.core.settings import settings

# ── Config (read lazily from settings, not os.environ at import time) ─────────

def _cf_account_id()  -> str: return settings.CF_ACCOUNT_ID
def _cf_api_token()   -> str: return settings.CF_API_TOKEN
def _d1_database_id() -> str: return settings.D1_DATABASE_ID
def _r2_endpoint()    -> str: return settings.R2_ENDPOINT_URL
def _r2_bucket()      -> str: return settings.R2_BUCKET_NAME

def _d1_query_url() -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{_cf_account_id()}"
        f"/d1/database/{_d1_database_id()}/query"
    )

def _d1_headers() -> dict:
    return {
        "Authorization": f"Bearer {_cf_api_token()}",
        "Content-Type": "application/json",
    }


# ── D1 Client ─────────────────────────────────────────────────────────────────

class D1Client:
    """Async wrapper around the Cloudflare D1 REST API."""

    def __init__(self):
        self._http = httpx.AsyncClient(timeout=30)

    async def query(self, sql: str, params: list = None) -> list[dict]:
        """Execute a SELECT query and return a list of row dicts."""
        payload = {"sql": sql, "params": params or []}
        resp = await self._http.post(_d1_query_url(), headers=_d1_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"D1 query error: {data.get('errors')}")
        return data["result"][0].get("results", [])

    async def execute(self, sql: str, params: list = None) -> dict:
        """Execute INSERT / UPDATE / DELETE. Returns meta (rows_written etc.)."""
        payload = {"sql": sql, "params": params or []}
        resp = await self._http.post(_d1_query_url(), headers=_d1_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"D1 execute error: {data.get('errors')}")
        return data["result"][0].get("meta", {})

    async def close(self):
        await self._http.aclose()


# ── R2 Client (singleton) ─────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _r2_client():
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=_r2_endpoint(),
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            # R2 sends CRC32 checksums that older botocore validates incorrectly
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


# ── Chapter Text: R2 Read / Write ─────────────────────────────────────────────

def upload_chapter_text_to_r2(novel_slug: str, chapter_num: int, text: str) -> str:
    """
    Compress and upload chapter text to R2.
    Paragraphs must be separated by double newlines (\\n\\n) before calling.
    Returns the R2 key.
    """
    r2_key = f"novels/{novel_slug}/chapter_{chapter_num}.txt.gz"
    compressed = gzip.compress(text.encode("utf-8"), compresslevel=9)
    _r2_client().put_object(
        Bucket=_r2_bucket(),
        Key=r2_key,
        Body=compressed,
        ContentType="text/plain",
        ContentEncoding="gzip",
    )
    return r2_key


def read_chapter_text_from_r2(r2_key: str) -> str:
    """
    Download chapter from R2 and return raw text.
    Handles both gzip-compressed and plain UTF-8 files gracefully.
    Paragraphs are separated by \\n\\n exactly as they were stored.
    """
    try:
        obj = _r2_client().get_object(Bucket=_r2_bucket(), Key=r2_key)
        raw = obj["Body"].read()
        # Try gzip first; fall back to plain text
        try:
            return gzip.decompress(raw).decode("utf-8")
        except (gzip.BadGzipFile, OSError):
            return raw.decode("utf-8")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise FileNotFoundError(f"Chapter not found in R2: {r2_key}")
        raise


def get_chapter_paragraphs(r2_key: str) -> list[str]:
    """
    Read chapter from R2 and return a list of paragraph strings.
    Each element is one paragraph; order is preserved exactly.
    """
    text = read_chapter_text_from_r2(r2_key)
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def reconstruct_chapter_text(r2_key: str) -> str:
    """Read chapter from R2 and return full text with paragraphs as \\n\\n."""
    paragraphs = get_chapter_paragraphs(r2_key)
    return "\n\n".join(paragraphs)


# Audio is generated on-the-fly via TTS — not stored in R2.
