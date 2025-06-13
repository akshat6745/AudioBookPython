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
                source="epub_upload"
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
            total_chapters = novel_doc.get("chapterCount")
            total_pages = (total_chapters + 49) // 50  # Round up division
            
            return {
                "chapters": chapters,
                "total_pages": total_pages,
                "current_page": page
            }
        
        # If not found in Firestore, try fetching from novelfire
        url = f"https://novelfire.net/book/{novel_name}/chapters"
        if page > 1:
            url += f"?page={page}"
        for _ in range(3):
            try:
                async with session.get(url, headers=get_headers(), ssl=False) as response:
                    response.raise_for_status()
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    chapters = []
                    chapter_elements = soup.select('.chapters-list li') or soup.select('.chapter-list .item') or soup.select('.chapter-item')
                    if not chapter_elements:
                        chapter_elements = soup.select('.chapter-list-item') or soup.select('table.chapter-table tr') or soup.select('.list-item')
                    for i, element in enumerate(chapter_elements):
                        a_tag = element.find('a')
                        if a_tag:
                            chapter_text = a_tag.text.strip()
                            chapter_link = a_tag['href']
                            if not chapter_link.startswith('http'):
                                chapter_link = f"https://novelfire.net{chapter_link}"
                            chapter_number = None
                            chapter_title = chapter_text
                            match = re.search(r'chapter-(\d+)', chapter_link)
                            if match:
                                chapter_number = int(match.group(1))
                            if not chapter_number:
                                chapter_number = (page - 1) * 50 + i + 1
                            chapter_info = {
                                "chapterNumber": chapter_number,
                                "chapterTitle": chapter_title,
                                "link": chapter_link
                            }
                            chapters.append(chapter_info)
                    total_pages = 1
                    pagination = soup.select('.pagination') or soup.select('.pages') or soup.select('.page-list')
                    if pagination:
                        page_links = pagination[0].select('a')
                        page_numbers = []
                        for link in page_links:
                            page_match = re.search(r'page=(\d+)', link.get('href', ''))
                            if page_match:
                                page_numbers.append(int(page_match.group(1)))
                            elif link.text.strip().isdigit():
                                page_numbers.append(int(link.text.strip()))
                        if page_numbers:
                            total_pages = max(page_numbers)
                    if total_pages == 1:
                        last_page = soup.select_one('.pagination a:last-child') or soup.select_one('.page-item:last-child a')
                        if last_page and last_page.get('href'):
                            page_match = re.search(r'page=(\d+)', last_page.get('href', ''))
                            if page_match:
                                total_pages = int(page_match.group(1))
                    chapter_count_elem = soup.select_one('.chapter-count') or soup.select_one('.total-chapters')
                    total_chapters = None
                    if chapter_count_elem:
                        count_match = re.search(r'of\s+(\d+)', chapter_count_elem.text)
                        if count_match:
                            total_chapters = int(count_match.group(1))
                            chapters_per_page = len(chapters)
                            if chapters_per_page > 0:
                                total_pages = max(total_pages, (total_chapters + chapters_per_page - 1) // chapters_per_page)
                    if chapters:
                        return {
                            "chapters": chapters,
                            "total_pages": total_pages,
                            "current_page": page
                        }
                    for a_tag in soup.find_all('a', href=True):
                        href = a_tag['href']
                        if 'chapter-' in href:
                            chapter_text = a_tag.text.strip()
                            chapter_link = href if href.startswith('http') else f"https://novelfire.net{href}"
                            match = re.search(r'chapter-(\d+)', href)
                            if match:
                                chapter_number = int(match.group(1))
                                chapter_info = {
                                    "chapterNumber": chapter_number,
                                    "chapterTitle": chapter_text,
                                    "link": chapter_link
                                }
                                chapters.append(chapter_info)
                    if chapters:
                        return {
                            "chapters": chapters,
                            "total_pages": total_pages,
                            "current_page": page
                        }
                    raise HTTPException(status_code=500, detail="Could not parse chapter list")
            except Exception as e:
                continue
        raise HTTPException(status_code=500, detail="Failed to fetch chapters after multiple attempts")
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
            
        # Rest of the existing novelfire parsing code
        soup = BeautifulSoup(html, 'html.parser')
        content_div = (
            soup.find('div', {'class': 'chapter-content'}) or
            soup.find('div', {'id': 'chapter-content'}) or
            soup.find('div', {'class': 'text-left'}) or
            soup.find('div', {'class': 'chapter-content-inner'}) or
            soup.select_one('div.elementor-widget-container')
        )
        chapter_title = soup.find('h1') or soup.find('title')
        chapter_title_text = chapter_title.text.strip() if chapter_title else "Unknown Title"
        if content_div:
            paragraphs = [p.text.strip() for p in content_div.find_all('p') if p.text.strip()]
            if not paragraphs:
                paragraphs = [text.strip() for text in content_div.stripped_strings if text.strip()]
            if paragraphs:
                return {
                    "chapterNumber": chapterNumber,
                    "chapterTitle": chapter_title_text,
                    "content": paragraphs
                }
        main_content = soup.find('main') or soup.find('article') or soup.body
        if main_content:
            paragraphs = [p.text.strip() for p in main_content.find_all('p') if p.text.strip()]
            paragraphs = [p for p in paragraphs if not p.startswith("If you find any errors") and not p.startswith("Search the NovelFire.net")]
            if paragraphs:
                return {
                    "chapterNumber": chapterNumber,
                    "chapterTitle": chapter_title_text,
                    "content": paragraphs
                }
        raise HTTPException(status_code=500, detail="Could not find chapter content")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching chapter content: {str(e)}")

@router.get("/novel-with-tts")
async def novel_with_tts(novelName: str, chapterNumber: int, voice: str, dialogueVoice: str):
    try:
        # Use fetch_chapter to get parsed chapter
        chapter = await fetch_chapter(chapterNumber, novelName)
        paragraphs = chapter.get("content", [])
        if not paragraphs:
            raise HTTPException(status_code=404, detail="Chapter content not found")

        # Prepare async TTS tasks for each paragraph
        async def tts_paragraph(paragraph):
            return await text_to_speech_dual_voice(paragraph, voice, dialogueVoice)

        tasks = [tts_paragraph(p) for p in paragraphs]
        audio_responses = await asyncio.gather(*tasks)

        # Combine all mp3 audio pieces in order
        combined_audio = io.BytesIO()
        for resp in audio_responses:
            # Each resp is a StreamingResponse, so we need to extract the audio bytes
            # We'll assume the response body is a BytesIO or similar
            if hasattr(resp, 'body_iterator'):
                async for chunk in resp.body_iterator:
                    combined_audio.write(chunk)
            elif hasattr(resp, 'body'):
                combined_audio.write(await resp.body())
            elif hasattr(resp, 'getvalue'):
                combined_audio.write(resp.getvalue())

        combined_audio.seek(0)
        return StreamingResponse(
            combined_audio,
            media_type="audio/mp3",
            headers={
                "Content-Disposition": f"attachment; filename=chapter_{chapterNumber}.mp3",
                "Cache-Control": "no-cache"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating TTS: {str(e)}")