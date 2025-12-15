from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import List, Optional
from bs4 import BeautifulSoup
from app.core.config import DOC_ID
from app.core.utils import get_headers
import aiohttp
import re
from fastapi.responses import StreamingResponse
from app.api.tts import generate_audio, text_to_speech_dual_voice
import io
import asyncio

router = APIRouter()

session = None  # This will be set by the main app

from app.models.schemas import NovelInfo
from app.core.db import db

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
        paragraphs = []
        
        # Strategy: Parse the HTML more carefully to handle mixed content
        # Remove the title element first to avoid including it
        title_elem = content_div.find('h1')
        if title_elem:
            title_elem.decompose()
        
        # Method 1: Try to get paragraphs from <p> tags
        p_elements = content_div.find_all('p')
        p_texts = []
        for p in p_elements:
            text = p.get_text().strip()
            if text:
                p_texts.append(text)
        
        # Method 2: Get all remaining text content after removing <p> tags
        # Clone the content_div to avoid modifying the original
        content_copy = content_div.__copy__()
        
        # Remove all <p> tags from the copy
        for p in content_copy.find_all('p'):
            p.decompose()
        
        # Get remaining text content
        remaining_text = content_copy.get_text().strip()
        remaining_paragraphs = []
        
        if remaining_text:
            # Split by newlines and clean up
            lines = remaining_text.split('\n')
            for line in lines:
                line = line.strip()
                if line and len(line) > 10:  # Only substantial content
                    remaining_paragraphs.append(line)
        
        # Combine both methods
        paragraphs = p_texts + remaining_paragraphs
        
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
    try:
        # Fetch novels from Google Doc
        url = f"https://docs.google.com/document/d/{DOC_ID}/export?format=txt"
        async with session.get(url, headers=get_headers()) as response:
            response.raise_for_status()
            text = await response.text()
        doc_novels = [NovelInfo(
            id=None,
            title=line.strip(),
            author=None,
            chapterCount=None,
            source="google_doc"
        ) for line in text.split('\n') if line.strip()]
        
        # Fetch novels from Firestore
        db_novels = []
        for doc in novels_collection.stream():
            data = doc.to_dict()
            db_novels.append(NovelInfo(
                id=doc.id,
                title=data.get("title"),
                author=data.get("author"),
                chapterCount=data.get("chapterCount"),
                source="epub_upload",
                hasImages=data.get("hasImages", False),
                imageCount=data.get("imageCount", 0)
            ))
        
        # Combine both sources
        return doc_novels + db_novels
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching novels: {str(e)}")

@router.get("/chapters-with-pages/{novel_name}")
async def fetch_chapters_with_pages(novel_name: str, page: Optional[int] = 1):
    try:
        # First try to find the novel in Firestore
        novels = novels_collection.where("title", "==", novel_name).stream()
        novel_doc = next(novels, None)
        
        if novel_doc:
            # Novel exists in Firestore, fetch its chapters
            chapters_collection = novel_doc.reference.collection("chapters")
            # Calculate pagination
            start = (page - 1) * 50
            end = start + 50
            
            # Query chapters in order
            chapters_query = chapters_collection.order_by("chapterNumber").offset(start).limit(50)
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
            total_pages = (total_chapters + 49) // 50  # Round up division
            
            return {
                "chapters": chapters,
                "total_pages": total_pages,
                "current_page": page
            }
        
        # If not found in Firestore, try fetching from novelfire
        # New approach: Use their internal AJAX API
        try:
            # 1. Get the chapters page to find the post_id
            chapters_url = f"https://novelfire.net/book/{novel_name}/chapters"
            async with session.get(chapters_url, headers=get_headers(), ssl=False) as response:
                response.raise_for_status()
                html = await response.text()
            
            # 2. Extract post_id
            match = re.search(r'post_id=(\d+)', html)
            if not match:
                raise HTTPException(status_code=500, detail="Could not find novel ID (post_id) for external fetch")
            
            post_id = match.group(1)
            
            # 3. Call the AJAX endpoint
            # Calculate pagination params
            start = (page - 1) * 50
            length = 50
            
            ajax_url = f"https://novelfire.net/listChapterDataAjax?post_id={post_id}&draw=1&start={start}&length={length}"
            
            async with session.get(ajax_url, headers=get_headers(), ssl=False) as ajax_response:
                ajax_response.raise_for_status()
                data = await ajax_response.json()
                
                # 4. Parse response
                chapters = []
                for item in data.get('data', []):
                    # Item format: {'n_sort': 1, 'slug': 'chapter-1-...', 'title': 'Chapter 1: ...', ...}
                    chapter_number = item.get('n_sort')
                    chapter_title = item.get('title')
                    slug = item.get('slug')
                    
                    # Ensure full link
                    chapter_link = f"https://novelfire.net/book/{novel_name}/{slug}"
                    
                    chapters.append({
                        "chapterNumber": int(chapter_number) if chapter_number else None,
                        "chapterTitle": chapter_title,
                        "link": chapter_link
                    })
                    
                # 5. Calculate totals
                total_records = int(data.get('recordsTotal', 0))
                total_pages = (total_records + 49) // 50
                
                if not chapters and page > 1 and page <= total_pages:
                     # If we got no chapters but page seems valid, something might be wrong, but let's return empty
                     pass
                     
                return {
                    "chapters": chapters,
                    "total_pages": total_pages,
                    "current_page": page
                }

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error fetching chapters from external source: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching chapters: {str(e)}")

@router.get("/chapter")
async def fetch_chapter(chapterNumber: int, novelName: str):
    try:
        # First try to find the novel in Firestore
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
        
        # If not found in Firestore, fetch from novelfire
        link = f"https://novelfire.net/book/{novelName}/chapter-{chapterNumber}"
        async with session.get(link, headers=get_headers(), ssl=False) as response:
            response.raise_for_status()
            html = await response.text()
            
        soup = BeautifulSoup(html, 'html.parser')
        chapter_title = soup.find('h1') or soup.find('title')
        chapter_title_text = chapter_title.text.strip() if chapter_title else "Unknown Title"
        
        # Use the new extract_paragraphs_from_soup function
        result = extract_paragraphs_from_soup(soup, chapter_title_text)
        return {
            "chapterNumber": chapterNumber,
            "chapterTitle": result["chapterTitle"],
            "content": result["content"]
        }
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