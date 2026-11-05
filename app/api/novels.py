from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from typing import List, Optional
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
from app.api.deps import AuthenticatedUser, get_current_user, get_optional_user
from app.core.rate_limit import rate_limit
from app.core.settings import settings
from app.core.config import DOC_ID
from app.core.utils import get_headers
from app.core.d1_client import get_d1_client
from app.services.cloudflare_service import get_chapter_paragraphs, _r2_client, _r2_bucket
from app.services.novel_store import cover_r2_key, generate_slug
import aiohttp
import re
from fastapi.responses import StreamingResponse
from app.api.tts import generate_audio, text_to_speech_dual_voice
import io
import asyncio
import structlog

router = APIRouter()
logger = structlog.get_logger()

_tts_rate_limit = rate_limit(
    "tts",
    limit=settings.TTS_RATE_LIMIT_REQUESTS,
    window_seconds=settings.TTS_RATE_LIMIT_WINDOW_SECONDS,
)

session = None  # This will be set by the main app

from app.models.schemas import NovelInfo

def extract_paragraphs_from_soup(soup: BeautifulSoup, chapter_title_text: str) -> dict:
    # ... (rest of the function remains the same, but I'll provide the whole block for replacement)
    """
    Extract paragraphs from BeautifulSoup object using various selectors.
    
    Args:
        soup: BeautifulSoup object of the parsed HTML
        chapter_title_text: The chapter title text
        
    Returns:
        dict: Contains chapterTitle and content (list of paragraphs)
        
    Raises:
        HTTPException: If no content could be found
    """
    # Try different content div selectors
    content_div = (
        soup.find('div', {'class': 'chapter-content'}) or
        soup.find('div', {'id': 'chapter-content'}) or
        soup.find('div', {'class': 'text-left'}) or
        soup.find('div', {'class': 'chapter-content-inner'}) or
        soup.select_one('div.elementor-widget-container')
    )
    
    if content_div:
        # Remove the title element first to avoid including it
        title_elem = content_div.find('h1')
        if title_elem:
            title_elem.decompose()

        paragraphs = []
        
        # Helper to classify tags
        BLOCK_TAGS = {'p', 'div', 'section', 'article', 'aside', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'pre', 'hr', 'table', 'form'}

        def extract_content(element):
            collected_paragraphs = []
            current_buffer = []

            def flush_buffer():
                if current_buffer:
                    text = "".join(current_buffer).strip()
                    if text and len(text) > 1:
                        collected_paragraphs.append(text)
                    current_buffer.clear()

            for child in element.children:
                if isinstance(child, Comment):
                    continue
                if isinstance(child, NavigableString):
                    text = str(child)
                    if text.strip():
                        current_buffer.append(text)
                elif isinstance(child, Tag):
                    if child.name == 'br':
                        flush_buffer()
                    elif child.name == 'p':
                        flush_buffer()
                        text = child.get_text().strip()
                        if text:
                            collected_paragraphs.append(text)
                    elif child.name in BLOCK_TAGS:
                        flush_buffer()
                        collected_paragraphs.extend(extract_content(child))
                    else:
                        # Inline element (em, strong, span, a, etc.)
                        current_buffer.append(child.get_text())
            
            flush_buffer()
            return collected_paragraphs

        paragraphs = extract_content(content_div)
        
        # Remove duplicates while preserving order
        seen = set()
        unique_paragraphs = []
        for p in paragraphs:
            if p not in seen:
                seen.add(p)
                unique_paragraphs.append(p)
        
        if unique_paragraphs:
            return {
                "chapterTitle": chapter_title_text,
                "content": unique_paragraphs
            }
    
    # Fallback to main content areas
    main_content = soup.find('main') or soup.find('article') or soup.body
    if main_content:
        paragraphs = [p.text.strip() for p in main_content.find_all('p') if p.text.strip()]
        # Filter out unwanted paragraphs
        paragraphs = [p for p in paragraphs if not p.startswith("If you find any errors") and not p.startswith("Search the NovelFire.net")]
        if paragraphs:
            return {
                "chapterTitle": chapter_title_text,
                "content": paragraphs
            }
    
    raise HTTPException(status_code=500, detail="Could not find chapter content")

@router.get("/novels", response_model=List[NovelInfo])
async def fetch_names(caller: AuthenticatedUser = Depends(get_current_user)):
    """Fetch novels the caller has access to (their own + public).

    The library is scoped to the verified token, not to a username supplied by
    the caller — that parameter previously let anyone read anyone's library.
    If the caller uploaded their own copy of a novel whose title also exists in
    the public catalog, the caller sees the version they uploaded.
    """
    try:
        d1 = get_d1_client()
        username = caller.username
        user_id = await _resolve_caller_user_id(d1, username)

        rows = await d1.query(
            "SELECT n.id, n.id AS slug, n.title, n.author, n.description, "
            "n.total_chapters, n.is_public, n.user_id, up.updated_at AS last_read_date "
            "FROM novels n "
            "LEFT JOIN user_progress up "
            "  ON up.novel_id = n.id "
            " AND up.user_id = (SELECT id FROM users WHERE username = ? COLLATE NOCASE) "
            "WHERE n.user_id = (SELECT id FROM users WHERE username = ? COLLATE NOCASE) "
            "   OR n.is_public = 1 "
            "ORDER BY MAX(COALESCE(julianday(up.updated_at), 0), "
            "             COALESCE(julianday(n.created_at), 0)) DESC, "
            "         n.total_chapters DESC",
            [username, username],
        )

        # If the caller uploaded their own version of a novel, shadow any
        # unowned public duplicate with the same title so they see their own version.
        owned_titles = {
            (row.get("title") or "").strip().lower()
            for row in rows
            if user_id is not None and row.get("user_id") == user_id
        }

        visible_rows = [
            row
            for row in rows
            if (user_id is not None and row.get("user_id") == user_id)
            or (row.get("title") or "").strip().lower() not in owned_titles
        ]

        novels = [
            NovelInfo(
                id=str(row["id"]),
                slug=str(row["id"]),
                title=row["title"],
                author=row.get("author"),
                chapterCount=row.get("total_chapters"),
                source="cloudflare_d1",
                description=row.get("description"),
                isPublic=bool(row.get("is_public", 0)),
                isOwner=bool(user_id is not None and row.get("user_id") == user_id),
            )
            for row in visible_rows
        ]

        logger.info("Fetched novels from D1", count=len(novels), username=username)
        return novels
    except Exception as e:
        logger.error("Error fetching novels from D1", error=str(e))
        raise HTTPException(status_code=500, detail="Error fetching novels")


async def _resolve_caller_user_id(d1, username: Optional[str]) -> Optional[str]:
    if not username:
        return None
    rows = await d1.query(
        "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
        [username],
    )
    return rows[0]["id"] if rows else None


async def resolve_novel_id(
    d1, name: str, username: Optional[str] = None
) -> str:
    """Resolve a novel identifier (slug OR title) to a D1 novel ID (slug).

    When `username` is provided, resolution is scoped to novels visible to that
    user (their own uploads + public novels) and prefers the caller's own
    uploaded version when two users (or the public catalog) share a title.
    """
    user_id = await _resolve_caller_user_id(d1, username)

    if user_id is not None:
        # 1. Exact slug owned by the caller
        owned_exact = await d1.query(
            "SELECT id FROM novels WHERE id = ? AND user_id = ?",
            [name, user_id],
        )
        if owned_exact:
            return owned_exact[0]["id"]

        # 2. Title or base-slug match owned by the caller (so when 2 users upload
        #    the same book, each user resolves to the version they uploaded)
        owned_rows = await d1.query(
            "SELECT id, title FROM novels WHERE user_id = ?",
            [user_id],
        )
        needle = name.strip().lower()
        for row in owned_rows:
            title = (row.get("title") or "").strip()
            if title.lower() == needle or generate_slug(title) == needle:
                return row["id"]

        # 3. Public novel by exact slug
        pub_exact = await d1.query(
            "SELECT id FROM novels WHERE id = ? AND is_public = 1",
            [name],
        )
        if pub_exact:
            return pub_exact[0]["id"]

        # 4. Public novel by case-insensitive title
        pub_title = await d1.query(
            "SELECT id FROM novels WHERE LOWER(title) = LOWER(?) AND is_public = 1",
            [name],
        )
        if pub_title:
            return pub_title[0]["id"]

        raise HTTPException(status_code=404, detail=f"Novel '{name}' not found")

    # Unauthenticated fallback: only public or unowned novels
    rows = await d1.query("SELECT id, user_id, is_public FROM novels WHERE id = ?", [name])
    for row in rows:
        if row.get("is_public", 1) == 1 or row.get("user_id") is None:
            return row["id"]

    rows = await d1.query(
        "SELECT id, user_id, is_public FROM novels WHERE LOWER(title) = LOWER(?)", [name]
    )
    for row in rows:
        if row.get("is_public", 1) == 1 or row.get("user_id") is None:
            return row["id"]

    raise HTTPException(status_code=404, detail=f"Novel '{name}' not found")


@router.delete("/novel/{novel_id}")
@router.delete("/novels/{novel_id}")
async def delete_novel(
    novel_id: str,
    caller: AuthenticatedUser = Depends(get_current_user),
):
    """Delete a novel uploaded by the authenticated caller.

    Strictly scoped to the caller's `user_id` and the target novel's `id` so
    that if two users uploaded the same book, deleting one user's upload only
    removes their own copy (D1 rows + unshared R2 objects) and never touches
    another user's upload or public catalog novels.
    """
    d1 = get_d1_client()
    user_id = await _resolve_caller_user_id(d1, caller.username)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 1. Match by exact novel ID (slug) owned by this caller
    owned = await d1.query(
        "SELECT id, title, user_id, is_public, cover_url FROM novels "
        "WHERE id = ? AND user_id = ?",
        [novel_id, user_id],
    )

    # 2. If the caller passed a title or base slug instead of their suffixed
    #    slug (e.g. "My Book" when their slug is "my-book-1"), resolve among
    #    novels owned by this caller.
    if not owned:
        all_owned = await d1.query(
            "SELECT id, title, user_id, is_public, cover_url FROM novels WHERE user_id = ?",
            [user_id],
        )
        needle = novel_id.strip().lower()
        matches = [
            r
            for r in all_owned
            if (r.get("title") or "").strip().lower() == needle
            or generate_slug(r.get("title") or "") == needle
        ]
        if len(matches) > 1:
            slugs = ", ".join(r["id"] for r in matches)
            raise HTTPException(
                status_code=409,
                detail=f"Multiple uploaded novels match '{novel_id}'; specify the exact slug ({slugs})",
            )
        owned = matches

    if not owned:
        # Check if the novel is a public catalog book (which callers cannot delete)
        pub = await d1.query(
            "SELECT id, is_public, user_id FROM novels "
            "WHERE (id = ? OR LOWER(title) = LOWER(?)) AND is_public = 1",
            [novel_id, novel_id],
        )
        if pub:
            raise HTTPException(
                status_code=403,
                detail="You can only delete novels that you uploaded",
            )
        raise HTTPException(status_code=404, detail="Novel not found")

    target = owned[0]
    target_slug = target["id"]
    target_title = target["title"]

    try:
        chapter_rows = await d1.query(
            "SELECT r2_content_path, r2_audio_path FROM chapters WHERE novel_id = ?",
            [target_slug],
        )

        # Delete D1 rows strictly for this novel slug and owner
        await d1.execute("DELETE FROM user_progress WHERE novel_id = ?", [target_slug])
        await d1.execute("DELETE FROM chapters WHERE novel_id = ?", [target_slug])
        await d1.execute(
            "DELETE FROM novels WHERE id = ? AND user_id = ?",
            [target_slug, user_id],
        )

        # Collect R2 keys that belonged to this novel and verify no other novel
        # in D1 references them before deleting from R2.
        r2_keys_to_delete: list[str] = []
        for ch in chapter_rows:
            for col in ("r2_content_path", "r2_audio_path"):
                key = ch.get(col)
                if not key:
                    continue
                still_used = await d1.query(
                    f"SELECT 1 FROM chapters WHERE {col} = ? LIMIT 1",
                    [key],
                )
                if not still_used:
                    r2_keys_to_delete.append(key)

        for cover_key in {target.get("cover_url"), cover_r2_key(target_slug)}:
            if not cover_key:
                continue
            still_used = await d1.query(
                "SELECT 1 FROM novels WHERE cover_url = ? LIMIT 1",
                [cover_key],
            )
            if not still_used:
                r2_keys_to_delete.append(cover_key)

        if r2_keys_to_delete:
            def _delete_r2_keys(keys: list[str]) -> None:
                try:
                    s3 = _r2_client()
                    bucket = _r2_bucket()
                    if not s3 or not bucket:
                        return
                    for k in keys:
                        try:
                            s3.delete_object(Bucket=bucket, Key=k)
                        except Exception:
                            pass
                except Exception:
                    pass

            await run_in_threadpool(_delete_r2_keys, r2_keys_to_delete)

        get_chapter_paragraphs.cache_clear()
        logger.info(
            "Deleted novel",
            novel=target_slug,
            title=target_title,
            username=caller.username,
            r2_objects_deleted=len(r2_keys_to_delete),
        )
        return {
            "status": "success",
            "deleted": True,
            "novelId": target_slug,
            "title": target_title,
            "message": f"Novel '{target_title}' deleted successfully",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting novel", novel=target_slug, error=str(e))
        raise HTTPException(status_code=500, detail="Error deleting novel")


@router.get("/chapters-with-pages/{novel_name}")
async def fetch_chapters_with_pages(
    novel_name: str,
    page: Optional[int] = 1,
    caller: Optional[AuthenticatedUser] = Depends(get_optional_user),
):
    """Fetch paginated chapter list (metadata only) from Cloudflare D1."""
    try:
        d1 = get_d1_client()
        limit  = 100
        offset = (page - 1) * limit
        username = caller.username if isinstance(caller, AuthenticatedUser) else None

        novel_id = await resolve_novel_id(d1, novel_name, username=username)

        # total_chapters from novels table is a fast cached count
        novel_row = await d1.query("SELECT total_chapters FROM novels WHERE id = ?", [novel_id])
        total_chapters = novel_row[0].get("total_chapters", 0) if novel_row else 0
        total_pages    = max(1, (total_chapters + limit - 1) // limit)

        chapter_rows = await d1.query(
            "SELECT chapter_number, title AS chapter_title, word_count "
            "FROM chapters WHERE novel_id = ? "
            "ORDER BY chapter_number ASC LIMIT ? OFFSET ?",
            [novel_id, limit, offset],
        )

        chapters = [
            {
                "chapterNumber": ch["chapter_number"],
                "chapterTitle":  ch.get("chapter_title", f"Chapter {ch['chapter_number']}"),
                "id":            f"{novel_id}_ch_{ch['chapter_number']}",
                "wordCount":     ch.get("word_count"),
            }
            for ch in chapter_rows
        ]

        logger.info("Fetched chapters from D1", novel=novel_id, count=len(chapters))
        return {
            "chapters":     chapters,
            "total_pages":  total_pages,
            "current_page": page,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching chapters", novel=novel_name, error=str(e))
        raise HTTPException(status_code=500, detail="Error fetching chapters")


@router.get("/chapter")
async def fetch_chapter(
    chapterNumber: int,
    novelName: str,
    caller: Optional[AuthenticatedUser] = Depends(get_optional_user),
):
    """
    Fetch a single chapter's content.
    Metadata comes from Cloudflare D1; text content is streamed from R2.
    Accepts both slug ('shadow-slave') and title ('Shadow Slave').
    """
    try:
        d1 = get_d1_client()
        username = caller.username if isinstance(caller, AuthenticatedUser) else None
        novel_id = await resolve_novel_id(d1, novelName, username=username)

        rows = await d1.query(
            "SELECT chapter_number, title, r2_content_path "
            "FROM chapters WHERE novel_id = ? AND chapter_number = ?",
            [novel_id, chapterNumber],
        )

        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"Chapter {chapterNumber} not found for novel '{novelName}'",
            )

        row        = rows[0]
        r2_key     = row["r2_content_path"]
        chap_title = row.get("title", f"Chapter {chapterNumber}")

        # Fetch actual text from R2 (runs in thread pool to avoid blocking event loop)
        paragraphs = await asyncio.get_event_loop().run_in_executor(
            None, get_chapter_paragraphs, r2_key
        )

        logger.info("Fetched chapter from R2", novel=novelName, chapter=chapterNumber,
                    paragraphs=len(paragraphs))
        return {
            "chapterNumber": chapterNumber,
            "chapterTitle":  chap_title,
            "content":       paragraphs,
        }

    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Error fetching chapter", novel=novelName, chapter=chapterNumber, error=str(e))
        raise HTTPException(status_code=500, detail="Error fetching chapter content")


@router.get("/novel-with-tts")
async def novel_with_tts(
    novelName: str,
    chapterNumber: int,
    voice: str,
    dialogueVoice: str,
    caller: AuthenticatedUser = Depends(_tts_rate_limit),
):
    try:
        chapter = await fetch_chapter(chapterNumber, novelName)
        paragraphs    = chapter.get("content", [])
        chapter_title = chapter.get("chapterTitle", "Unknown Title")
        
        if not paragraphs:
            raise HTTPException(status_code=404, detail="Chapter content not found")

        async def collect_paragraph_chunks(text: str) -> list[bytes]:
            chunks = []
            async for chunk in text_to_speech_dual_voice(text, voice, dialogueVoice):
                chunks.append(chunk)
            return chunks

        async def audio_generator():
            # Launch all paragraphs in parallel up front. The semaphore inside
            # tts.py bounds true concurrency; we still yield in order so the
            # output MP3 is sequentially correct.
            items = [chapter_title, *paragraphs]
            tasks = [asyncio.create_task(collect_paragraph_chunks(t)) for t in items]
            try:
                for task in tasks:
                    for chunk in await task:
                        yield chunk
            except Exception:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                raise

        return StreamingResponse(
            audio_generator(),
            media_type="audio/mp3",
            headers={
                "Content-Disposition": f"attachment; filename=chapter_{chapterNumber}.mp3",
                "Cache-Control": "no-cache"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating TTS: {str(e)}")