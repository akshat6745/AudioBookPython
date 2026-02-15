"""
Alternative EPUB parser using SPINE-based extraction.
This serves as a validation method to compare against the TOC-based parser.

The spine represents the linear reading order of the book as defined by the EPUB standard,
while the TOC is a navigation aid that may not include all content.
"""

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
import re
import os
from typing import List, Dict, Tuple, Optional
import uuid
from app.models.schemas import Chapter, Novel

def parse_epub_content_spine(epub_content: bytes) -> Tuple[Novel, List[Dict], List[Dict]]:
    """
    Parse an EPUB file using the SPINE (reading order) instead of TOC.
    Returns a tuple of (Novel object, List of chapter dictionaries, empty list for images)
    
    This is an alternative parsing strategy for validation purposes.
    """
    # Create an EPUB book object from the bytes
    temp_path = f"/tmp/{uuid.uuid4()}.epub"
    with open(temp_path, "wb") as f:
        f.write(epub_content)
    
    try:
        book = epub.read_epub(temp_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    
    # Extract metadata
    title = book.get_metadata('DC', 'title')[0][0] if book.get_metadata('DC', 'title') else "Unknown Title"
    author = book.get_metadata('DC', 'creator')[0][0] if book.get_metadata('DC', 'creator') else "Unknown Author"
    
    # Extract chapters using the spine (reading order)
    firestore_chapters = extract_chapters_from_spine(book)
    
    chapters = [
        Chapter(number=c["chapterNumber"], title=c["chapterTitle"], content=c["content"])
        for c in firestore_chapters
    ]
    
    novel = Novel(
        title=title,
        author=author,
        chapters=chapters
    )
    
    # Images support removed - return empty list
    return novel, firestore_chapters, []

def extract_chapters_from_spine(book) -> List[Dict]:
    """
    Extract chapters from the EPUB's spine (linear reading order).
    The spine is the authoritative reading order defined in the EPUB package.
    """
    chapters = []
    chapter_number = 1
    
    # Get spine items (ordered reading sequence)
    spine = book.spine
    
    for item_id, _ in spine:
        try:
            # Get the item from the book
            item = book.get_item_with_id(item_id)
            
            if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            
            # Skip obvious non-chapter files
            file_name = item.file_name.lower()
            if any(skip in file_name for skip in ['cover', 'toc', 'nav', 'titlepage', 'copyright', 'title']):
                # But still process them if they have substantial content
                pass
            
            # Parse the HTML content
            soup = BeautifulSoup(item.content, 'html.parser')
            
            # Remove unwanted tags
            for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
                tag.decompose()
            
            # Extract title from the content
            title = extract_chapter_title_from_content(soup) or f"Chapter {chapter_number}"
            
            # Extract content
            content = extract_content_from_soup(soup)
            
            # Only add if there's substantial content (more than 50 chars)
            if content and len(''.join(content).strip()) > 50:
                chapters.append({
                    "chapterNumber": chapter_number,
                    "chapterTitle": title,
                    "content": content,
                    "images": [],  # Images support removed
                    "sourceFile": item.file_name  # For debugging/comparison
                })
                chapter_number += 1
                
        except Exception as e:
            print(f"Error processing spine item {item_id}: {e}")
            continue
    
    return chapters

def extract_chapter_title_from_content(soup: BeautifulSoup) -> str:
    """Extract chapter title from the HTML content."""
    # Try to find a heading tag
    for tag in ['h1', 'h2', 'h3', 'h4']:
        found = soup.find(tag)
        if found:
            title = found.get_text().strip()
            if title and len(title) > 0:
                return title[:200]
    
    # Try to find title in <title> tag
    title_tag = soup.find('title')
    if title_tag:
        title = title_tag.get_text().strip()
        if title and len(title) > 0:
            return title[:200]
    
    # Fallback to paragraph-based title detection
    for p in soup.find_all('p')[:5]:
        text = p.get_text().strip()
        # Look for chapter/prologue/epilogue patterns
        if re.match(r'^(chapter|prologue|epilogue|part|interlude|section|prelude)(\s+|:)', text, re.IGNORECASE):
            return text[:200]
    
    return ""

def extract_content_from_soup(soup: BeautifulSoup) -> List[str]:
    """Extract content from the soup."""
    # Get the container to extract from
    container = soup.find('body') or soup
    
    # Get children while filtering out NavigableStrings that are just whitespace
    children = [c for c in container.children if not (isinstance(c, NavigableString) and not str(c).strip())]
    
    return extract_content_from_elements(children)

def extract_content_from_elements(elements) -> List[str]:
    """Extract formatted content from a list of elements/tags."""
    content = []
    
    # Block tags that should trigger a new paragraph
    BLOCK_TAGS = {'p', 'div', 'section', 'article', 'aside', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li'}

    def process_element(element):
        local_content = []
        current_buffer = []

        def flush_buffer():
            if current_buffer:
                text = "".join(current_buffer).strip()
                if text:
                    local_content.append(re.sub(r'\s+', ' ', text))
                current_buffer.clear()

        # Handle direct children if it's a tag
        if hasattr(element, 'children'):
            for child in element.children:
                if isinstance(child, Comment):
                    continue
                if isinstance(child, NavigableString):
                    text = str(child).strip('\n\r')
                    if text:
                        current_buffer.append(text)
                elif isinstance(child, Tag):
                    if child.name == 'br':
                        flush_buffer()
                    elif child.name in BLOCK_TAGS:
                        flush_buffer()
                        local_content.extend(process_element(child))
                    else:
                        # Inline element (em, strong, span, etc.)
                        current_buffer.append(child.get_text())
            
            flush_buffer()
        return local_content

    for element in elements:
        if isinstance(element, Tag):
            content.extend(process_element(element))
        elif isinstance(element, NavigableString):
            text = str(element).strip()
            if text:
                content.append(re.sub(r'\s+', ' ', text))

    return content
