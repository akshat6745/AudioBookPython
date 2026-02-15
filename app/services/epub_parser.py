import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
import re
import os
from typing import List, Dict, Tuple, Optional
import uuid
from app.models.schemas import Chapter, Novel

def parse_epub_content(epub_content: bytes) -> Tuple[Novel, List[Dict], List[Dict]]:
    """
    Parse an EPUB file and extract its content using the table of contents.
    Returns a tuple of (Novel object, List of chapter dictionaries for Firestore, empty list for images)
    
    Images support has been removed. The third element will always be an empty list.
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
    
    # Extract chapters using the table of contents
    firestore_chapters = extract_chapters_from_toc(book)
    
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

def extract_chapters_from_toc(book) -> List[Dict]:
    """
    Extract chapters from the EPUB's table of contents.
    This flattens the nested TOC structure and extracts content for each entry.
    """
    toc = book.toc
    chapters = []
    chapter_number = 1
    
    # Flatten the TOC structure
    toc_entries = flatten_toc(toc)
    
    # Process each TOC entry
    for entry in toc_entries:
        try:
            # Get the title
            title = entry.title.strip() if hasattr(entry, 'title') else f"Chapter {chapter_number}"
            
            # Get the href (file path)
            href = entry.href if hasattr(entry, 'href') else None
            if not href:
                continue
            
            # Extract the file name from href (remove anchor if present)
            file_name = href.split('#')[0]
            
            # Find the corresponding item in the book
            item = None
            for book_item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                if book_item.file_name == file_name or book_item.file_name.endswith(file_name):
                    item = book_item
                    break
            
            if not item:
                continue
            
            # Parse the HTML content
            soup = BeautifulSoup(item.content, 'html.parser')
            
            # Remove unwanted tags
            for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
                tag.decompose()
            
            # If there's an anchor in the href, try to extract content from that point
            anchor = None
            if '#' in href:
                anchor = href.split('#')[1]
            
            # Extract content
            content = extract_content_from_soup(soup, anchor)
            
            if content and len(''.join(content).strip()) > 50:  # Only add if there's substantial content
                chapters.append({
                    "chapterNumber": chapter_number,
                    "chapterTitle": title,
                    "content": content,
                    "images": []  # Images support removed
                })
                chapter_number += 1
                
        except Exception as e:
            print(f"Error processing TOC entry: {e}")
            continue
    
    # If no chapters were extracted from TOC, fall back to file-based extraction
    if not chapters:
        chapters = fallback_file_based_extraction(book)
    
    return chapters

def flatten_toc(toc, result=None) -> List:
    """
    Recursively flatten the nested TOC structure.
    TOC can be a list of Links or tuples of (Link, [children]).
    """
    if result is None:
        result = []
    
    for item in toc:
        if isinstance(item, tuple):
            # This is a section with children: (section_link, [children])
            section, children = item
            if hasattr(section, 'href'):
                result.append(section)
            # Recursively process children
            if children:
                flatten_toc(children, result)
        elif hasattr(item, 'href'):
            # This is a direct link
            result.append(item)
    
    return result

def fallback_file_based_extraction(book) -> List[Dict]:
    """
    Fallback method to extract chapters when TOC is not available or empty.
    This uses the original file-based approach with improved title extraction.
    """
    chapters = []
    chapter_number = 1
    items = sorted(book.get_items_of_type(ebooklib.ITEM_DOCUMENT), key=lambda x: x.file_name)
    
    for item in items:
        # Skip obvious non-chapter files
        if any(p in item.file_name.lower() for p in ['cover', 'toc', 'nav', 'copyright', 'title']):
            continue
        
        soup = BeautifulSoup(item.content, 'html.parser')
        
        # Remove unwanted tags
        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
            tag.decompose()
        
        # Extract title
        title = extract_chapter_title(soup) or f"Chapter {chapter_number}"
        
        # Extract content
        content = extract_content_from_soup(soup)
        
        if content and len(''.join(content).strip()) > 100:
            chapters.append({
                "chapterNumber": chapter_number,
                "chapterTitle": title,
                "content": content,
                "images": []  # Images support removed
            })
            chapter_number += 1
    
    return chapters

def extract_chapter_title(soup: BeautifulSoup) -> str:
    """Extract chapter title from soup."""
    # Try to find a heading tag
    for tag in ['h1', 'h2', 'h3', 'h4']:
        found = soup.find(tag)
        if found:
            title = found.get_text().strip()
            if title:
                return title[:200]
    
    # Fallback to paragraph-based title detection
    for p in soup.find_all('p')[:5]:
        text = p.get_text().strip()
        if re.match(r'^(chapter|prologue|epilogue|part|interlude|section)(\\s+|$)', text, re.IGNORECASE):
            return text[:200]
    
    return ""

def extract_content_from_soup(soup: BeautifulSoup, anchor: Optional[str] = None) -> List[str]:
    """
    Extract content from the soup.
    If an anchor is provided, start extraction from that element.
    """
    # If there's an anchor, find the starting element
    start_element = None
    if anchor:
        start_element = soup.find(id=anchor) or soup.find('a', {'name': anchor})
    
    # Get the container to extract from
    if start_element:
        # Extract from the anchor point onwards
        container = start_element.parent if start_element.parent else soup
    else:
        # Extract from body or the whole soup
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
