from fastapi import APIRouter, HTTPException
from typing import List, Optional
from bs4 import BeautifulSoup
from app.core.config import DOC_ID
from app.core.utils import get_headers
import aiohttp
import re

router = APIRouter()

session = None  # This will be set by the main app

@router.get("/novels", response_model=List[str])
async def fetch_names():
    try:
        url = f"https://docs.google.com/document/d/{DOC_ID}/export?format=txt"
        async with session.get(url, headers=get_headers()) as response:
            response.raise_for_status()
            text = await response.text()
        novels = [line.strip() for line in text.split('\n') if line.strip()]
        return novels
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching novels: {str(e)}")

@router.get("/chapters-with-pages/{novel_name}")
async def fetch_chapters_with_pages(novel_name: str, page: Optional[int] = 1):
    try:
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
        link = f"https://novelfire.net/book/{novelName}/chapter-{chapterNumber}"
        async with session.get(link, headers=get_headers(), ssl=False) as response:
            response.raise_for_status()
            html = await response.text()
        soup = BeautifulSoup(html, 'html.parser')
        content_div = (
            soup.find('div', {'class': 'chapter-content'}) or
            soup.find('div', {'id': 'chapter-content'}) or
            soup.find('div', {'class': 'text-left'}) or
            soup.find('div', {'class': 'chapter-content-inner'}) or
            soup.select_one('div.elementor-widget-container')
        )
        if content_div:
            paragraphs = [p.text.strip() for p in content_div.find_all('p') if p.text.strip()]
            if not paragraphs:
                paragraphs = [text.strip() for text in content_div.stripped_strings if text.strip()]
            if paragraphs:
                return {"content": paragraphs}
        main_content = soup.find('main') or soup.find('article') or soup.body
        if main_content:
            paragraphs = [p.text.strip() for p in main_content.find_all('p') if p.text.strip()]
            paragraphs = [p for p in paragraphs if not p.startswith("If you find any errors") and not p.startswith("Search the NovelFire.net")]
            if paragraphs:
                return {"content": paragraphs}
        raise HTTPException(status_code=500, detail="Could not find chapter content")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching chapter content: {str(e)}") 