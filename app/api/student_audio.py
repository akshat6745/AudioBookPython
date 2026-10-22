"""Read-only stream of pre-rendered StyleTTS student audio.

Callers can read a stored file by its object path, or by novel, chapter, and
paragraph. Both require the same bearer token as the other API routes. The
paragraph form is what the StyleTTS voice sends:

    GET /student-audio/paragraph?novel=<slug>&chapter=<n>&paragraph=<p>

``paragraph=title`` is the chapter title, ``paragraph=<i>`` is paragraph i,
and ``paragraph=chapter`` is the whole-chapter file. The path form remains:

    GET /student-audio/audio-student/styletts2-eye-v1/<slug>/chapter_<n>/<file>

The object key passed to R2 is rebuilt from allowlisted segments. Production
``audio/`` and every other novel are refused before any storage call, and
this module never writes to R2 or synthesizes speech.
"""

import asyncio
import posixpath
import re

import structlog
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import AuthenticatedUser, get_current_user
from app.services.cloudflare_service import _r2_bucket, _r2_client

logger = structlog.get_logger()
router = APIRouter()

STUDENT_AUDIO_PREFIX = "audio-student/styletts2-eye-v1/"
STUDENT_AUDIO_NOVEL = "the-eye-of-the-world"
_NOT_AVAILABLE = "Audio not available"
_MISSING = "Recorded audio was not found"
_READ_FAILED = "Could not read recorded audio"
_CHUNK_SIZE = 64 * 1024

# Full match only. The novel, chapter, and filename are captured so the R2
# key can be assembled from those groups rather than from the raw request path.
_OBJECT_RE = re.compile(
    rf"^{re.escape(STUDENT_AUDIO_PREFIX)}"
    rf"(?P<slug>{re.escape(STUDENT_AUDIO_NOVEL)})/"
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
    paragraph, and ``chapter`` selects the whole-chapter file. Another novel,
    a filename, or any other paragraph value is refused before storage.
    """
    if (
        not isinstance(novel, str)
        or not isinstance(chapter, str)
        or not isinstance(paragraph, str)
    ):
        _refuse()
    if novel != STUDENT_AUDIO_NOVEL:
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
        "Cache-Control": "private, max-age=86400",
        # Existing audio routes do not implement Range. Do not advertise it.
        "Accept-Ranges": "none",
        "X-Content-Type-Options": "nosniff",
    }
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
    return await stream_allowlisted_student_audio(key)


@router.get("/student-audio/{object_key:path}")
async def get_student_audio(
    object_key: str,
    caller: AuthenticatedUser = Depends(get_current_user),
):
    """Stream one allowlisted student-audio object. Read-only."""
    key = validate_student_audio_key(object_key)
    return await stream_allowlisted_student_audio(key)
