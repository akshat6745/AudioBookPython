from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import List, Optional
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
from app.core.config import DOC_ID
from app.core.utils import get_headers
from app.core.supabase_client import get_supabase_client
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
from app.core.db import db
from firebase_admin import firestore


novels_collection = db.collection("novels")

def extract_paragraphs_from_soup(soup: BeautifulSoup, chapter_title_text: str) -> dict:
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
        # Fallback also needs to support loose text if possible, but safe to keep simple for now
        # Or better yet, use the same extract_blocks strategy on the main_content!
        # But previous logic was specific: find_all('p') and filter.
        # Let's keep previous fallback logic to minimize regression risk on generic sites
        # unless we want to standardize.
        # Existing logic:
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
    """Fetch novels from Supabase only - optimized for speed."""
    try:
        supabase = get_supabase_client()
        # Simple query to novels table only - no joins
        result = supabase.table('novels').select(
            'id, slug, title, author, genres, status, description'
        ).order('updated_at', desc=True).execute()
        
        novels = [
            NovelInfo(
                id=str(row['id']),
                slug=row['slug'],
                title=row['title'],
                author=row.get('author'),
                chapterCount=None,  # Not fetching chapter count for speed
                source="supabase",
                status=row.get('status'),
                genres=row.get('genres'),
                description=row.get('description')
            )
            for row in result.data
        ]
        
        logger.info("Fetched novels from Supabase", count=len(novels))
        return novels
            
    except Exception as e:
        logger.error("Error fetching novels from Supabase", error=str(e))
        raise HTTPException(status_code=500, detail=f"Error fetching novels: {str(e)}")

@router.get("/chapters-with-pages/{novel_name}")
async def fetch_chapters_with_pages(novel_name: str, page: Optional[int] = 1):
    """Fetch paginated chapters for a novel from Supabase or Firebase."""
    try:
        # 1. First try Supabase (novel_name is the slug)
        try:
            supabase = get_supabase_client()
            
            # Get the novel by slug
            novel_result = supabase.table('novels').select('id').eq('slug', novel_name).single().execute()
            
            if novel_result.data:
                novel_id = novel_result.data['id']
                
                # Calculate pagination (100 chapters per page, ascending order - chapter 1 first)
                start = (page - 1) * 100
                end = start + 99  # Supabase range is inclusive
                
                # Get chapters with pagination
                chapters_result = supabase.table('chapters').select(
                    'id, chapter_number, chapter_title, word_count'
                ).eq('novel_id', novel_id).order(
                    'chapter_number', desc=False
                ).range(start, end).execute()
                
                # Get total chapter count
                total_result = supabase.table('chapters').select(
                    'id', count='exact'
                ).eq('novel_id', novel_id).execute()
                
                total_chapters = total_result.count if total_result.count else 0
                total_pages = (total_chapters + 99) // 100
                
                chapters = []
                for ch in chapters_result.data:
                    chapters.append({
                        "chapterNumber": ch['chapter_number'],
                        "chapterTitle": ch['chapter_title'],
                        "id": str(ch['id']),
                        "wordCount": ch.get('word_count')
                    })
                
                logger.info("Fetched chapters from Supabase", novel=novel_name, count=len(chapters))
                return {
                    "chapters": chapters,
                    "total_pages": total_pages,
                    "current_page": page
                }
        except Exception as e:
            # Log but continue to try Firebase
            logger.debug("Novel not found in Supabase, trying Firebase", novel=novel_name, error=str(e))
        
        # 2. Try Firebase (for EPUB uploads - novel_name is the title)
        novels = novels_collection.where("title", "==", novel_name).stream()
        novel_doc = next(novels, None)
        
        if novel_doc:
            # Novel exists in Firestore, fetch its chapters
            chapters_collection = novel_doc.reference.collection("chapters")
            # Calculate pagination
            start = (page - 1) * 100
            
            # Query chapters in order (ascending - chapter 1 first)
            chapters_query = chapters_collection.order_by(
                "chapterNumber", direction=firestore.Query.ASCENDING
            ).offset(start).limit(100)
            chapters = []
            
            for chapter_doc in chapters_query.stream():
                chapter_data = chapter_doc.to_dict()
                chapter_info = {
                    "chapterNumber": chapter_data["chapterNumber"],
                    "chapterTitle": chapter_data["chapterTitle"],
                    "id": chapter_doc.id
                }
                chapters.append(chapter_info)
            
            # Get total chapter count from the novel document
            novel_data = novel_doc.to_dict()
            total_chapters = novel_data.get("chapterCount", 0)
            total_pages = (total_chapters + 99) // 100  # Round up division
            
            return {
                "chapters": chapters,
                "total_pages": total_pages,
                "current_page": page
            }
        
        # Novel not found in either source
        raise HTTPException(status_code=404, detail=f"Novel '{novel_name}' not found")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching chapters: {str(e)}")

@router.get("/chapter")
async def fetch_chapter(chapterNumber: int, novelName: str):
    """Fetch a single chapter's content from Supabase or Firebase."""
    try:
        # 1. First try Supabase (novelName is the slug)
        try:
            supabase = get_supabase_client()
            
            # Get the novel by slug
            novel_result = supabase.table('novels').select('id').eq('slug', novelName).single().execute()
            
            if novel_result.data:
                novel_id = novel_result.data['id']
                
                # Get the chapter
                chapter_result = supabase.table('chapters').select(
                    'chapter_number, chapter_title, content, word_count'
                ).eq('novel_id', novel_id).eq('chapter_number', chapterNumber).single().execute()
                
                if chapter_result.data:
                    ch = chapter_result.data
                    # Content is already a TEXT[] array in Supabase - no parsing needed!
                    content = ch['content'] if ch['content'] else []
                    
                    logger.info("Fetched chapter from Supabase", novel=novelName, chapter=chapterNumber)
                    return {
                        "chapterNumber": ch['chapter_number'],
                        "chapterTitle": ch['chapter_title'],
                        "content": content
                    }
        except Exception as e:
            # Log but continue to try Firebase
            logger.debug("Chapter not found in Supabase, trying Firebase", novel=novelName, chapter=chapterNumber, error=str(e))
        
        # 2. Try Firebase (for EPUB uploads - novelName is the title)
        novels = novels_collection.where("title", "==", novelName).stream()
        novel_doc = next(novels, None)
        
        if novel_doc:
            # Novel exists in Firestore, fetch the specific chapter
            chapter_doc = novel_doc.reference.collection("chapters").document(str(chapterNumber)).get()
            
            if chapter_doc.exists:
                chapter_data = chapter_doc.to_dict()
                return {
                    "chapterNumber": chapterNumber,
                    "chapterTitle": chapter_data.get("chapterTitle", "Unknown Title"),
                    "content": chapter_data.get("content", [])
                }
        
        # Chapter not found in either source
        raise HTTPException(status_code=404, detail=f"Chapter {chapterNumber} not found for novel '{novelName}'")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching chapter content: {str(e)}")

@router.get("/novel-with-tts")
async def novel_with_tts(novelName: str, chapterNumber: int, voice: str, dialogueVoice: str):
    try:
        # Use fetch_chapter to get parsed chapter
        chapter = await fetch_chapter(chapterNumber, novelName)
        paragraphs = chapter.get("content", [])
        chapter_title = chapter.get("chapterTitle", "Unknown Title")
        
        if not paragraphs:
            raise HTTPException(status_code=404, detail="Chapter content not found")

        async def audio_generator():
            # 1. Generate audio for chapter title
            async for chunk in text_to_speech_dual_voice(chapter_title, voice, dialogueVoice):
                yield chunk
            
            # 2. Generate audio for each paragraph sequentially
            # Note: We process sequentially to stream in order. 
            # Parallel processing would require buffering which defeats the purpose of streaming for low memory.
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