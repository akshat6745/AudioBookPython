from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from bs4 import BeautifulSoup
import aiohttp
import requests
import tempfile
import asyncio
import edge_tts
import io
from typing import List, Dict, Optional
import os
import re
from dotenv import load_dotenv
from fake_useragent import UserAgent
from contextlib import asynccontextmanager

import undetected_chromedriver as uc


# Load environment variables
load_dotenv()

# Initialize session
session = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create session
    global session
    session = aiohttp.ClientSession()
    yield
    # Shutdown: cleanup
    if session:
        await session.close()

app = FastAPI(title="Novel Reader API", lifespan=lifespan)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create headers with rotating user agent
def get_headers():
    ua = UserAgent()
    return {
        "User-Agent": ua.random,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

# Google Doc ID from environment variables
DOC_ID = os.getenv('SHEET_ID')

@app.get("/novels", response_model=List[str])
async def fetch_names():
    """
    Fetch all novel names from the Google Doc
    """
    try:
        # Access the document as a webpage
        url = f"https://docs.google.com/document/d/{DOC_ID}/export?format=txt"
        async with session.get(url, headers=get_headers()) as response:
            response.raise_for_status()
            text = await response.text()

        # Split the text into lines and remove empty lines
        novels = [line.strip() for line in text.split('\n') if line.strip()]
        return novels
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching novels: {str(e)}")

@app.get("/chapters-with-pages/{novel_name}")
async def fetch_chapters_with_pages(novel_name: str, page: Optional[int] = 1):
    """
    Fetch chapters for a specific novel with pagination from novelfire.net
    """
    try:
        # Format the URL with the novel name and page number
        url = f"https://novelfire.net/book/{novel_name}/chapters"
        if page > 1:
            url += f"?page={page}"
        
        print(f"Fetching chapters from: {url}")

        # Try multiple times with different user agents if needed
        for _ in range(3):
            try:
                async with session.get(url, headers=get_headers(), ssl=False) as response:
                    response.raise_for_status()
                    html = await response.text()

                    soup = BeautifulSoup(html, 'html.parser')
                    chapters = []
                    
                    # Find the chapters list - usually in a table or list
                    chapter_elements = soup.select('.chapters-list li') or soup.select('.chapter-list .item') or soup.select('.chapter-item')
                    
                    if not chapter_elements:
                        # Try other common selectors for chapter lists
                        chapter_elements = soup.select('.chapter-list-item') or soup.select('table.chapter-table tr') or soup.select('.list-item')
                    
                    # Extract chapter information from each element
                    for i, element in enumerate(chapter_elements):
                        a_tag = element.find('a')
                        if a_tag:
                            # Extract chapter number and title
                            chapter_text = a_tag.text.strip()
                            chapter_link = a_tag['href']
                            
                            # Convert relative URLs to absolute
                            if not chapter_link.startswith('http'):
                                chapter_link = f"https://novelfire.net{chapter_link}"
                            
                            # Some sites have separate elements for chapter number and title
                            chapter_number = None
                            chapter_title = chapter_text
                            
                            # Try to extract chapter number from link
                            match = re.search(r'chapter-(\d+)', chapter_link)
                            if match:
                                chapter_number = int(match.group(1))
                            
                            # If we couldn't extract chapter number from URL, use position+offset
                            if not chapter_number:
                                chapter_number = (page - 1) * 50 + i + 1  # Assuming 50 chapters per page
                            
                            chapter_info = {
                                "chapterNumber": chapter_number,
                                "chapterTitle": chapter_title,
                                "link": chapter_link
                            }
                            chapters.append(chapter_info)

                    # Determine total number of pages from pagination links
                    total_pages = 1  # Default to 1 if no pagination found
                    
                    # Look for pagination elements
                    pagination = soup.select('.pagination') or soup.select('.pages') or soup.select('.page-list')
                    if pagination:
                        # Find all page number links
                        page_links = pagination[0].select('a')
                        page_numbers = []
                        for link in page_links:
                            # Extract page number from href or text
                            page_match = re.search(r'page=(\d+)', link.get('href', ''))
                            if page_match:
                                page_numbers.append(int(page_match.group(1)))
                            elif link.text.strip().isdigit():
                                page_numbers.append(int(link.text.strip()))
                        
                        if page_numbers:
                            total_pages = max(page_numbers)
                    
                    # If we still haven't found pages, try to find the last page button
                    if total_pages == 1:
                        last_page = soup.select_one('.pagination a:last-child') or soup.select_one('.page-item:last-child a')
                        if last_page and last_page.get('href'):
                            page_match = re.search(r'page=(\d+)', last_page.get('href', ''))
                            if page_match:
                                total_pages = int(page_match.group(1))
                    
                    # Find total chapter count if available (sometimes sites show "1-100 of 362 chapters")
                    chapter_count_elem = soup.select_one('.chapter-count') or soup.select_one('.total-chapters')
                    total_chapters = None
                    if chapter_count_elem:
                        count_match = re.search(r'of\s+(\d+)', chapter_count_elem.text)
                        if count_match:
                            total_chapters = int(count_match.group(1))
                            # Estimate total pages based on chapters per page
                            chapters_per_page = len(chapters)
                            if chapters_per_page > 0:
                                total_pages = max(total_pages, (total_chapters + chapters_per_page - 1) // chapters_per_page)

                    if chapters:  # If we found chapters, return them with total_pages info
                        return {
                            "chapters": chapters,
                            "total_pages": total_pages,
                            "current_page": page
                        }
                    
                    # If we didn't find chapters using selectors, try a more generic approach
                    print("Trying generic selectors for chapter list")
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
                    
                    if chapters:  # If we found chapters with the generic approach
                        return {
                            "chapters": chapters,
                            "total_pages": total_pages,
                            "current_page": page
                        }
                    
                    print(f"Could not extract chapters from HTML. Site structure may have changed.")
                    raise HTTPException(status_code=500, detail="Could not parse chapter list")
                    
            except Exception as e:
                print(f"Attempt failed: {e}")
                continue

        # If we get here, all attempts failed
        raise HTTPException(status_code=500, detail="Failed to fetch chapters after multiple attempts")

    except Exception as e:
        print(f"Error fetching chapters: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching chapters: {str(e)}")

@app.get("/chapter")
async def fetch_chapter(chapterNumber: int, novelName: str):
    """
    Fetch content of a specific chapter
    """
    try:
        link = f"https://novelfire.net/book/{novelName}/chapter-{chapterNumber}"
        print(f"Fetching: {link}")

        async with session.get(link, headers=get_headers(), ssl=False) as response:
            response.raise_for_status()
            html = await response.text()

        soup = BeautifulSoup(html, 'html.parser')

        # Try different selectors to find the chapter content
        content_div = (
            soup.find('div', {'class': 'chapter-content'}) or
            soup.find('div', {'id': 'chapter-content'}) or
            soup.find('div', {'class': 'text-left'}) or
            soup.find('div', {'class': 'chapter-content-inner'}) or
            soup.select_one('div.elementor-widget-container')
        )

        if content_div:
            # Find all paragraphs in the chapter content
            paragraphs = [p.text.strip() for p in content_div.find_all('p') if p.text.strip()]

            if not paragraphs:
                # If no paragraphs found, try getting direct text
                paragraphs = [text.strip() for text in content_div.stripped_strings if text.strip()]

            if paragraphs:  # If we found content, return it
                return {"content": paragraphs}

        # If we reached here, we didn't find the content with our selectors
        # Let's get the page source and look for more clues
        print(f"Could not extract content with standard selectors from: {link}")

        # Try alternative approach - some sites load content differently
        # Return broader content for debugging
        main_content = soup.find('main') or soup.find('article') or soup.body
        if main_content:
            paragraphs = [p.text.strip() for p in main_content.find_all('p') if p.text.strip()]
            if paragraphs:
                return {"content": paragraphs}

        raise HTTPException(
            status_code=500,
            detail="Could not find chapter content"
        )

    except Exception as e:
        print(f"Error fetching chapter: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching chapter content: {str(e)}")

class TTSRequest(BaseModel):
    text: str
    voice: str = "en-US-ChristopherNeural"  # Default voice

@app.post("/tts")
async def text_to_speech_post(request: TTSRequest = None, text: str = None, voice: str = "en-US-ChristopherNeural"):
    """
    Convert text to speech using Edge TTS and stream binary data directly
    Accepts either a JSON body or query parameters
    """
    return await process_tts_request(request, text, voice)

@app.get("/tts")
async def text_to_speech_get(text: str, voice: str = "en-US-ChristopherNeural"):
    """
    GET endpoint for text-to-speech conversion
    """
    return await process_tts_request(None, text, voice)

async def process_tts_request(request: TTSRequest = None, text: str = None, voice: str = "en-US-ChristopherNeural"):
    """
    Common processing function for TTS requests
    """
    try:
        # Use request body if provided, otherwise use query parameters
        if request:
            text_to_convert = request.text
            voice_to_use = request.voice
        else:
            if not text:
                raise HTTPException(status_code=400, detail="Text parameter is required if not using JSON body")
            text_to_convert = text
            voice_to_use = voice
            
        # Preprocess the text to handle special patterns
        # Replace ellipses and asterisks with appropriate speech text
        text_to_convert = text_to_convert.replace("...", " pause ")
        text_to_convert = text_to_convert.replace("***", " break ")
        
        # Handle empty or too short text after preprocessing
        if not text_to_convert or text_to_convert.isspace():
            text_to_convert = ""
            
        # Stream audio data directly to memory without using temporary files
        communicate = edge_tts.Communicate(text_to_convert, voice_to_use)
        
        # Create an in-memory buffer
        output = io.BytesIO()
        
        # Stream audio data directly to the buffer
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                output.write(chunk["data"])
        
        # Reset buffer position to the beginning
        output.seek(0)
        
        # Return the audio data as a streaming response
        return StreamingResponse(
            output, 
            media_type="audio/mp3",
            headers={
                "Content-Disposition": "attachment; filename=speech.mp3",
                "Cache-Control": "no-cache"
            }
        )
    except Exception as e:
        print(f"Error in text-to-speech conversion: {e}")
        raise HTTPException(status_code=500, detail=f"Error in text-to-speech conversion: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)