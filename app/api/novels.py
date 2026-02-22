from fastapi import APIRouter, HTTPException
from typing import List, Optional
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
from app.core.config import DOC_ID
from app.core.utils import get_headers
from app.core.d1_client import get_d1_client
from app.services.cloudflare_service import get_chapter_paragraphs
import aiohttp
import re
from fastapi.responses import StreamingResponse
from app.api.tts import generate_audio, text_to_speech_dual_voice
import io
import asyncio
import structlog

router = APIRouter()
logger = structlog.get_logger()

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
async def fetch_names():
    """Fetch all novels from Cloudflare D1."""
    try:
        d1 = get_d1_client()
        rows = await d1.query(
            "SELECT id, id AS slug, title, author, description, total_chapters "
            "FROM novels ORDER BY total_chapters DESC"
        )

        novels = [
            NovelInfo(
                id=str(row["id"]),
                slug=str(row["id"]),        # slug == id in D1
                title=row["title"],
                author=row.get("author"),
                chapterCount=row.get("total_chapters"),
                source="cloudflare_d1",
                description=row.get("description"),
            )
            for row in rows
        ]

        logger.info("Fetched novels from D1", count=len(novels))
        return novels
    except Exception as e:
        logger.error("Error fetching novels from D1", error=str(e))
        raise HTTPException(status_code=500, detail=f"Error fetching novels: {str(e)}")


async def resolve_novel_id(d1, name: str) -> str:
    """
    Resolve a novel identifier (slug OR title) to a D1 novel ID (slug).
    Tries exact slug match first, then case-insensitive title fallback.
    Raises 404 if not found.
    """
    # 1. Try slug (exact)
    rows = await d1.query("SELECT id FROM novels WHERE id = ?", [name])
    if rows:
        return rows[0]["id"]

    # 2. Try title (case-insensitive) — for old frontend clients
    rows = await d1.query(
        "SELECT id FROM novels WHERE LOWER(title) = LOWER(?)", [name]
    )
    if rows:
        return rows[0]["id"]

    raise HTTPException(status_code=404, detail=f"Novel '{name}' not found")


@router.get("/chapters-with-pages/{novel_name}")
async def fetch_chapters_with_pages(novel_name: str, page: Optional[int] = 1):
    """Fetch paginated chapter list (metadata only) from Cloudflare D1."""
    try:
        d1 = get_d1_client()
        limit  = 100
        offset = (page - 1) * limit

        novel_id = await resolve_novel_id(d1, novel_name)

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
        raise HTTPException(status_code=500, detail=f"Error fetching chapters: {str(e)}")


@router.get("/chapter")
async def fetch_chapter(chapterNumber: int, novelName: str):
    """
    Fetch a single chapter's content.
    Metadata comes from Cloudflare D1; text content is streamed from R2.
    Accepts both slug ('shadow-slave') and title ('Shadow Slave').
    """
    try:
        d1 = get_d1_client()
        novel_id = await resolve_novel_id(d1, novelName)

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
        raise HTTPException(status_code=500, detail=f"Error fetching chapter content: {str(e)}")


@router.get("/novel-with-tts")
async def novel_with_tts(novelName: str, chapterNumber: int, voice: str, dialogueVoice: str):
    try:
        chapter = await fetch_chapter(chapterNumber, novelName)
        paragraphs    = chapter.get("content", [])
        chapter_title = chapter.get("chapterTitle", "Unknown Title")
        
        if not paragraphs:
            raise HTTPException(status_code=404, detail="Chapter content not found")

        async def audio_generator():
            async for chunk in text_to_speech_dual_voice(chapter_title, voice, dialogueVoice):
                yield chunk
            for paragraph in paragraphs:
                async for chunk in text_to_speech_dual_voice(paragraph, voice, dialogueVoice):
                    yield chunk

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