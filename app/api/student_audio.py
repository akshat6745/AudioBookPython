"""Read-only stream of pre-rendered StyleTTS student audio.

Callers can read a stored file by its object path, or by novel, chapter, and
paragraph. Both require the same bearer token as the other API routes. The
paragraph form is what the StyleTTS voice sends:

    GET /student-audio/paragraph?novel=<slug>&chapter=<n>&paragraph=<p>

``paragraph=title`` is the chapter title, ``paragraph=<i>`` is paragraph i,
and ``paragraph=chapter`` is the whole-chapter file. The path form remains:

    GET /student-audio/audio-student/styletts2-eye-v1/<slug>/chapter_<n>/<file>

The object key passed to R2 is rebuilt from allowlisted segments. Any novel
slug (lowercase letters, digits, single hyphens) is accepted under the student
prefix, since the local StyleTTS Live engine saves chapters for every novel in
the same layout. Production ``audio/`` and every other prefix are refused
before any storage call, and this module never writes to R2 or synthesizes
speech.
"""

import asyncio
import posixpath
import re

import structlog
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import AuthenticatedUser, get_current_user
from app.core.d1_client import get_d1_client
from app.core.settings import settings
from app.services.cloudflare_service import _r2_bucket, _r2_client
from app.services.novel_store import generate_slug

logger = structlog.get_logger()
router = APIRouter()

STUDENT_AUDIO_PREFIX = "audio-student/styletts2-eye-v1/"
_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_SLUG_RE = re.compile(rf"^{_SLUG}$")
_NOT_AVAILABLE = "Audio not available"
_MISSING = "Recorded audio was not found"
_READ_FAILED = "Could not read recorded audio"
_CHUNK_SIZE = 64 * 1024

# Full match only. The novel, chapter, and filename are captured so the R2
# key can be assembled from those groups rather than from the raw request path.
_OBJECT_RE = re.compile(
    rf"^{re.escape(STUDENT_AUDIO_PREFIX)}"
    rf"(?P<slug>{_SLUG})/"
    r"(?P<chapter>chapter_[1-9][0-9]*)/"
    r"(?P<filename>manifest\.json|title\.opus|chapter\.opus|[0-9]+\.opus)$"
)

# Paragraph-style parameters. These are roles and indexes, not filenames.
_CHAPTER_PARAM_RE = re.compile(r"^[1-9][0-9]*$")
_PARAGRAPH_PARAM_RE = re.compile(r"^(?:title|chapter|0|[1-9][0-9]*)$")


def _refuse() -> None:
    raise HTTPException(status_code=403, detail=_NOT_AVAILABLE)


def validate_student_audio_key(raw_key: str) -> str:
    """Return a rebuilt allowlisted R2 key, or 403.

    Relative segments are rejected before normalization so they cannot be
    collapsed into a different prefix. The value sent to R2 is constructed
    from the allowlisted capture groups and checked against the prefix again.
    """
    if not isinstance(raw_key, str) or not raw_key or raw_key != raw_key.strip():
        _refuse()
    if "\x00" in raw_key or "\\" in raw_key or "://" in raw_key:
        _refuse()
    if raw_key.startswith("/") or raw_key.startswith("./"):
        _refuse()

    parts = raw_key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _refuse()

    match = _OBJECT_RE.fullmatch(raw_key)
    if match is None:
        _refuse()

    key = (
        f"{STUDENT_AUDIO_PREFIX}"
        f"{match.group('slug')}/{match.group('chapter')}/{match.group('filename')}"
    )
    normalized = posixpath.normpath(key)
    if (
        normalized != key
        or not normalized.startswith(STUDENT_AUDIO_PREFIX)
        or posixpath.commonpath([normalized, STUDENT_AUDIO_PREFIX.rstrip("/")])
        != STUDENT_AUDIO_PREFIX.rstrip("/")
    ):
        _refuse()
    return normalized


def content_type_for_student_audio(key: str) -> str:
    """Content type the player expects. Opus is served as audio/ogg."""
    filename = posixpath.basename(key)
    if filename.endswith(".opus") and _OBJECT_RE.fullmatch(key):
        return "audio/ogg"
    if filename == "manifest.json" and _OBJECT_RE.fullmatch(key):
        return "application/json"
    _refuse()
    raise AssertionError("unreachable")


def filename_for_paragraph_param(paragraph: str) -> str:
    """Map a paragraph parameter onto the stored Opus name."""
    if paragraph == "title":
        return "title.opus"
    if paragraph == "chapter":
        return "chapter.opus"
    return f"{paragraph}.opus"


def student_audio_key_from_params(novel: str, chapter: str, paragraph: str) -> str:
    """Return the allowlisted key for novel, chapter, and paragraph.

    ``title`` selects the chapter title, a decimal index selects that
    paragraph, and ``chapter`` selects the whole-chapter file. A malformed
    novel slug, a filename, or any other paragraph value is refused before
    storage.
    """
    if (
        not isinstance(novel, str)
        or not isinstance(chapter, str)
        or not isinstance(paragraph, str)
    ):
        _refuse()
    if _SLUG_RE.fullmatch(novel) is None:
        _refuse()
    if _CHAPTER_PARAM_RE.fullmatch(chapter) is None:
        _refuse()
    if _PARAGRAPH_PARAM_RE.fullmatch(paragraph) is None:
        _refuse()
    filename = filename_for_paragraph_param(paragraph)
    return validate_student_audio_key(
        f"{STUDENT_AUDIO_PREFIX}{novel}/chapter_{chapter}/{filename}"
    )


def _missing_object(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    return code in {"NoSuchKey", "404", "NotFound"}


async def _resolve_caller_student_slug(slug: str, caller: AuthenticatedUser) -> str:
    """Resolve `slug` against the caller's owned novels in D1 when available.

    - If the caller owns a novel whose base title slug matches `slug` (for
      example, User B uploaded 'Dune' and got slug 'dune-1', while a client
      derived 'dune' from the title), return the caller's own slug ('dune-1').
    - If `slug` in D1 belongs to another user's private upload (`is_public = 0`),
      refuse with 404 so one user never reads another user's private audio.
    - If D1 is not configured (e.g. unit tests with a fake R2 only) or the slug
      is not in D1, return `slug` unchanged.
    """
    if not (
        getattr(settings, "CF_ACCOUNT_ID", None)
        and getattr(settings, "D1_DATABASE_ID", None)
        and getattr(settings, "CF_API_TOKEN", None)
    ):
        return slug

    try:
        d1 = get_d1_client()
        user_rows = await d1.query(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            [caller.username],
        )
        if not user_rows:
            return slug
        caller_user_id = user_rows[0]["id"]

        owned_rows = await d1.query(
            "SELECT id, title FROM novels WHERE user_id = ?",
            [caller_user_id],
        )
        for row in owned_rows:
            if row["id"] == slug:
                return slug
        for row in owned_rows:
            title = (row.get("title") or "").strip()
            if title and generate_slug(title) == slug:
                return row["id"]

        novel_rows = await d1.query(
            "SELECT id, user_id, is_public FROM novels WHERE id = ?",
            [slug],
        )
        if novel_rows:
            row = novel_rows[0]
            is_public = bool(row.get("is_public", 0))
            owner_id = row.get("user_id")
            if not is_public and owner_id is not None and owner_id != caller_user_id:
                raise HTTPException(status_code=404, detail=_MISSING)
    except HTTPException:
        raise
    except Exception:
        # Best-effort D1 check; do not break playback if D1 is unreachable
        pass

    return slug


async def stream_allowlisted_student_audio(key: str) -> StreamingResponse:
    """Stream an already-validated student-audio key. Read-only."""
    media_type = content_type_for_student_audio(key)
    try:
        obj = await asyncio.to_thread(
            _r2_client().get_object, Bucket=_r2_bucket(), Key=key
        )
    except ClientError as exc:
        if _missing_object(exc):
            raise HTTPException(status_code=404, detail=_MISSING)
        logger.warning(
            "student_audio_read_failed",
            error_code=exc.response.get("Error", {}).get("Code"),
        )
        raise HTTPException(status_code=502, detail=_READ_FAILED)
    except HTTPException:
        raise
    except Exception:
        logger.warning("student_audio_read_failed")
        raise HTTPException(status_code=502, detail=_READ_FAILED)

    body = obj["Body"]
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        # Existing audio routes do not implement Range. Do not advertise it.
        "Accept-Ranges": "none",
        "X-Content-Type-Options": "nosniff",
    }
    etag = obj.get("ETag")
    if isinstance(etag, str) and etag:
        headers["ETag"] = etag
    length = obj.get("ContentLength")
    if isinstance(length, int) and length >= 0:
        headers["Content-Length"] = str(length)

    async def chunks():
        try:
            while True:
                chunk = await asyncio.to_thread(body.read, _CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                await asyncio.to_thread(close)

    return StreamingResponse(chunks(), media_type=media_type, headers=headers)


@router.get("/student-audio/paragraph")
async def get_student_audio_paragraph(
    novel: str,
    chapter: str,
    paragraph: str,
    caller: AuthenticatedUser = Depends(get_current_user),
):
    """Stream the stored Opus file for one novel, chapter, and paragraph.

    Registered ahead of the path route so ``paragraph`` is not captured as
    an object key. Read-only.
    """
    key = student_audio_key_from_params(novel, chapter, paragraph)
    resolved_slug = await _resolve_caller_student_slug(novel, caller)
    if resolved_slug != novel:
        key = student_audio_key_from_params(resolved_slug, chapter, paragraph)
    return await stream_allowlisted_student_audio(key)


@router.get("/student-audio/{object_key:path}")
async def get_student_audio(
    object_key: str,
    caller: AuthenticatedUser = Depends(get_current_user),
):
    """Stream one allowlisted student-audio object. Read-only."""
    key = validate_student_audio_key(object_key)
    match = _OBJECT_RE.fullmatch(key)
    if match is not None:
        slug = match.group("slug")
        resolved_slug = await _resolve_caller_student_slug(slug, caller)
        if resolved_slug != slug:
            key = validate_student_audio_key(
                f"{STUDENT_AUDIO_PREFIX}{resolved_slug}/{match.group('chapter')}/{match.group('filename')}"
            )
    return await stream_allowlisted_student_audio(key)
