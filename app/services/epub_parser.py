import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import re
from typing import List, Dict, Tuple
import io
import base64
import uuid
from app.models.schemas import Chapter, Novel

def parse_epub_content(epub_content: bytes) -> Tuple[Novel, List[Dict], List[Dict]]:
    """
    Parse an EPUB file and extract its content including images.
    Returns a tuple of (Novel object, List of chapter dictionaries for Firestore, List of image dictionaries)
    
    This parser splits content by h2 headings within each HTML file to handle
    EPUBs where multiple chapters/sections are combined in single files.
    """
    # Save the content for debugging
    with open("/tmp/debug_epub.epub", "wb") as f:
        f.write(epub_content)
    
    # Create an EPUB book object from the bytes
    epub_file = io.BytesIO(epub_content)
    book = epub.read_epub("/tmp/debug_epub.epub")
    
    # Extract metadata
    title = book.get_metadata('DC', 'title')[0][0] if book.get_metadata('DC', 'title') else "Unknown Title"
    author = book.get_metadata('DC', 'creator')[0][0] if book.get_metadata('DC', 'creator') else "Unknown Author"
    
    # Extract and store images
    images = extract_images_from_epub(book)
    
    chapters = []
    firestore_chapters = []
    chapter_number = 1
    
    # Sort items by file name to maintain order
    items = sorted(book.get_items_of_type(ebooklib.ITEM_DOCUMENT), key=lambda x: x.file_name)
    
    for item in items:
        if not is_chapter_content(item.file_name, item.content):
            continue
            
        soup = BeautifulSoup(item.content, 'html.parser')
        
        # Remove unwanted tags
        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
            tag.decompose()
        
        # Find all h2 headings - each represents a separate chapter/section
        h2_tags = soup.find_all('h2')
        
        if h2_tags:
            # Split by h2 headings
            for i, h2 in enumerate(h2_tags):
                chapter_title = h2.get_text().strip()
                
                if not chapter_title:
                    continue
                
                # Extract content between this h2 and the next h2 (or end of file)
                content = []
                current = h2.find_next_sibling()
                
                while current:
                    if current.name == 'h2':
                        break  # Stop at next h2
                    
                    # Extract text from paragraphs and other text elements
                    if current.name == 'p':
                        text = current.get_text().strip()
                        if text and len(text) > 10:
                            text = re.sub(r'\s+', ' ', text)
                            content.append(text)
                    elif current.name in ['div', 'blockquote', 'section']:
                        # Handle nested content
                        for p in current.find_all('p'):
                            text = p.get_text().strip()
                            if text and len(text) > 10:
                                text = re.sub(r'\s+', ' ', text)
                                content.append(text)
                    
                    current = current.find_next_sibling()
                
                if not content:
                    continue
                
                # Create chapter objects
                chapter = Chapter(
                    number=chapter_number,
                    title=chapter_title,
                    content=content
                )
                
                firestore_chapter = {
                    "chapterNumber": chapter_number,
                    "chapterTitle": chapter_title,
                    "content": content,
                    "images": []
                }
                
                chapters.append(chapter)
                firestore_chapters.append(firestore_chapter)
                chapter_number += 1
        else:
            # No h2 tags - treat the entire file as one chapter (fallback)
            chapter_title = extract_chapter_title(soup) or f"Chapter {chapter_number}"
            content, chapter_images = extract_chapter_content_with_images(soup, images)
            
            if not content:
                continue
            
            chapter = Chapter(
                number=chapter_number,
                title=chapter_title,
                content=content
            )
            
            firestore_chapter = {
                "chapterNumber": chapter_number,
                "chapterTitle": chapter_title,
                "content": content,
                "images": chapter_images
            }
            
            chapters.append(chapter)
            firestore_chapters.append(firestore_chapter)
            chapter_number += 1
    
    novel = Novel(
        title=title,
        author=author,
        chapters=chapters
    )
    
    return novel, firestore_chapters, images


def extract_images_from_epub(book) -> List[Dict]:
    """
    Extract all images from the EPUB file.
    Returns a list of image dictionaries with metadata and base64 data.
    """
    images = []
    
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        try:
            # Generate unique ID for the image
            image_id = str(uuid.uuid4())
            
            # Get image metadata
            file_name = item.file_name
            content_type = item.media_type or 'image/jpeg'
            
            # Convert image to base64 for storage
            image_data = base64.b64encode(item.content).decode('utf-8')
            
            image_info = {
                "id": image_id,
                "originalPath": file_name,
                "contentType": content_type,
                "size": len(item.content),
                "data": image_data  # Base64 encoded image data
            }
            
            images.append(image_info)
            
        except Exception as e:
            print(f"Error processing image {item.file_name}: {e}")
            continue
    
    return images

def is_chapter_content(file_name: str, content: bytes) -> bool:
    """
    Check if the file is likely to be a chapter by analyzing both filename and content.
    Hybrid approach: uses proven logic for common cases, with robust fallbacks for edge cases.
    """
    file_name_lower = file_name.lower()
    
    # Always exclude certain patterns in filename
    exclude_patterns = ['cover', 'toc', 'contents', 'copyright', 'title', 'thank', 'oceanof']
    if any(pattern in file_name_lower for pattern in exclude_patterns):
        return False
    
    try:
        # Basic size checks
        if len(content) < 300:
            return False
            
        soup = BeautifulSoup(content, 'html.parser')
        text_content = soup.get_text().strip()
        
        if len(text_content) < 200:
            return False
        
        # PROVEN LOGIC (from original working version) - handle common cases first
        first_300_chars = text_content.lower()[:300]
        
        # Strong exclusions (high confidence)
        # Use ratio-based TOC detection instead of absolute count
        # TOC pages have many "Chapter" mentions but short content overall
        # A real chapter with 20,000+ words that mentions "chapter" 50 times is NOT a TOC
        # A TOC with 500 words and 50 "chapter" mentions IS a TOC
        chapter_count = text_content.lower().count('chapter')
        words = len(text_content.split())
        if words > 0 and chapter_count > 12:
            chapter_density = chapter_count / words
            # If more than 1 "chapter" per 50 words on average, likely TOC
            if chapter_density > 0.02:
                return False
        if 'contents' in text_content.lower()[:200] and 'prologue' in text_content.lower()[:200]:
            return False
        if any(word in text_content.lower()[:200] for word in ['glossary', 'about the author', 'acknowledgments']):
            return False
        
        # Enhanced preview detection (more specific)
        preview_patterns = [
            r'preview of .+ book', r'book \w+ of .+ series', r'coming in book \w+',
            r'next book.*:', r'continues in.*book'
        ]
        if any(re.search(pattern, text_content[:500].lower()) for pattern in preview_patterns):
            return False
        
        # Strong positive indicators (high confidence)
        # Use \s* instead of \s+ to handle text-runs like "chapter1title" from p-tag extraction
        has_chapter_number = bool(re.search(r'chapter\s*\d+', first_300_chars))
        has_prologue = 'prologue' in first_300_chars
        has_epilogue = 'epilogue' in first_300_chars
        
        # If we have strong indicators and good length, likely a chapter
        if (has_chapter_number or has_prologue or has_epilogue) and len(text_content) > 500:
            return True
        
        # ROBUST FALLBACKS (for edge cases and different formats)
        # Use scoring for less clear cases
        score = 0
        
        # Broader chapter patterns for different formats
        extended_patterns = [
            r'chapter\s*[ivxlc]+',  # Roman numerals
            r'ch\.\s*\d+',          # Abbreviated
            r'part\s*\d+',          # Parts
            r'interlude',           # Interludes
        ]
        
        for pattern in extended_patterns:
            if re.search(pattern, first_300_chars):
                score += 2
        
        # Story content indicators
        story_patterns = [
            r'\b(he|she|they)\s+(said|walked|ran|looked|saw)',
            r'(once upon|in the|it was|there was)',
        ]
        
        for pattern in story_patterns:
            if re.search(pattern, first_300_chars):
                score += 1
                break
        
        # Length considerations
        if len(text_content) > 1500:
            score += 1
        elif len(text_content) < 600:  # Very short content needs strong indicators
            return score >= 3
        
        # Decision for edge cases
        return score >= 2
            
    except Exception:
        # Simple fallback: if content is substantial, probably a chapter
        return len(content) > 800

def extract_chapter_title(soup: BeautifulSoup) -> str:
    """
    Extract chapter title from the HTML content.
    More robust to handle different EPUB formatting styles including:
    - Standard headings (h1, h2, h3)
    - Multi-paragraph chapter headers (<p>CHAPTER</p><p>21</p><p>Title</p>)
    - Single paragraph titles
    """
    # First, try common heading tags
    for tag in ['h1', 'h2', 'h3', 'h4']:
        heading = soup.find(tag)
        if heading:
            title = heading.get_text().strip()
            if len(title) > 2 and title.lower() not in ['chapter', 'the', 'a', 'an']:
                title = re.sub(r'^Chapter\s*(\d+)\s*[-:]\s*', r'Chapter \1: ', title, flags=re.IGNORECASE)
                title = re.sub(r'^Chapter\s*(\d+)\s*$', r'Chapter \1', title, flags=re.IGNORECASE)
                return title[:200]  # Limit length
    
    # Try paragraph-based extraction for EPUBs like Eye of the World
    # Format: <p>CHAPTER</p><p>21</p><p>Listen to the Wind</p>
    paragraphs = soup.find_all('p')[:10]  # Check first 10 paragraphs
    para_texts = [p.get_text().strip() for p in paragraphs if p.get_text().strip()]
    
    if para_texts:
        # Look for CHAPTER, PROLOGUE, or EPILOGUE in first few paragraphs
        for i, text in enumerate(para_texts[:5]):
            text_upper = text.upper()
            if text_upper in ['CHAPTER', 'PROLOGUE', 'EPILOGUE', 'PART', 'INTERLUDE']:
                title_parts = [text]
                # Collect following short parts (number and title)
                for j in range(i + 1, min(i + 4, len(para_texts))):
                    next_text = para_texts[j]
                    if len(next_text) > 100:  # Too long, this is content not title
                        break
                    # Skip book title noise
                    if any(skip in next_text.lower() for skip in ['wheel of time', 'book 1', 'book 2']):
                        continue
                    title_parts.append(next_text)
                    # If we got a chapter number + title, stop
                    if len(title_parts) >= 3:
                        break
                return ' '.join(title_parts)[:200]
            
            # Check for "CHAPTER 1" or "Chapter 1: Title" format in single paragraph
            if re.match(r'^(chapter|prologue|epilogue)\s+\d*', text, re.IGNORECASE):
                return text[:200]
    
    # Fallback to text-line based extraction
    text_content = soup.get_text().strip()
    lines = [line.strip() for line in text_content.split('\n') if line.strip()]
    
    strategies = [
        extract_multiline_chapter_title,
        extract_single_line_chapter_title,
        extract_pattern_based_title,
        extract_numeric_title
    ]
    
    for strategy in strategies:
        title = strategy(lines)
        if title:
            return title[:200]  # Limit length
    
    # Final fallback: try finding chapter headings in paragraphs
    for p in soup.find_all('p'):
        text = p.get_text().strip()
        if re.match(r'^(chapter|prologue|epilogue|part|interlude)\s+', text, re.IGNORECASE):
            return text[:200]
            
    return ""


def extract_multiline_chapter_title(lines: list) -> str:
    """Extract title from multi-line chapter headers"""
    chapter_line_idx = None
    
    # Look for chapter keywords
    for i, line in enumerate(lines[:15]):
        line_upper = line.upper()
        
        if line_upper in ['CHAPTER', 'PROLOGUE', 'EPILOGUE', 'INTERLUDE']:
            chapter_line_idx = i
            keyword = line_upper
            break
        elif re.match(r'^(PART|BOOK)\s*$', line_upper):
            chapter_line_idx = i
            keyword = line_upper
            break
    
    if chapter_line_idx is not None:
        title_parts = [keyword]
        
        # Look for number/title in next few lines
        for j in range(chapter_line_idx + 1, min(chapter_line_idx + 6, len(lines))):
            if j >= len(lines):
                break
                
            line = lines[j]
            
            # Skip very short lines or lines that are just numbers
            if len(line) <= 2:
                if line.isdigit() or re.match(r'^[ivxlc]+$', line.lower()):
                    title_parts.append(line)
                continue
            
            # Skip book title repetitions
            if any(skip_word in line.lower() for skip_word in ['the eye of the world', 'wheel of time']):
                continue
                
            # This looks like a chapter title
            if len(line) > 2:
                title_parts.append(line)
                break
                
        return ' '.join(title_parts)
    
    return ""

def extract_single_line_chapter_title(lines: list) -> str:
    """Extract title from single-line chapter headers"""
    for line in lines[:10]:
        # Various single-line patterns
        patterns = [
            r'^(CHAPTER\s+\d+.*?)$',
            r'^(CHAPTER\s+[IVXLC]+.*?)$',
            r'^(CH\.\s*\d+.*?)$',
            r'^(PROLOGUE.*?)$',
            r'^(EPILOGUE.*?)$',
            r'^(INTERLUDE.*?)$',
            r'^(PART\s+\d+.*?)$',
            r'^(PART\s+[IVXLC]+.*?)$',
        ]
        
        for pattern in patterns:
            match = re.match(pattern, line, re.IGNORECASE)
            if match:
                return match.group(1).strip()
    
    return ""

def extract_pattern_based_title(lines: list) -> str:
    """Extract title using pattern matching in any line"""
    for line in lines[:20]:
        # Look for chapter patterns anywhere in the line
        patterns = [
            r'(chapter\s+\d+[^a-z]*[a-z][^.]*)',
            r'(prologue[^a-z]*[a-z][^.]*)',
            r'(epilogue[^a-z]*[a-z][^.]*)',
            r'(part\s+\d+[^a-z]*[a-z][^.]*)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, line.lower())
            if match:
                # Get the original case version
                start, end = match.span(1)
                return line[start:end].strip()
    
    return ""

def extract_numeric_title(lines: list) -> str:
    """Fallback: create title from numbers found"""
    for i, line in enumerate(lines[:10]):
        if re.match(r'^\d+$', line):
            # Found a standalone number, assume it's a chapter number
            return f"Chapter {line}"
        elif re.match(r'^[ivxlc]+$', line.lower()):
            # Roman numeral
            return f"Chapter {line.upper()}"
    
    return ""

def extract_chapter_content_with_images(soup: BeautifulSoup, epub_images: List[Dict]) -> Tuple[List[str], List[str]]:
    """
    Extract and clean chapter content, returning a list of paragraphs and image references.
    """
    # Remove navigation elements
    for nav in soup.find_all(['nav', 'header', 'footer']):
        nav.decompose()
    
    # Create a mapping of original image paths to our image IDs
    image_path_map = {img["originalPath"]: img["id"] for img in epub_images}
    
    # Get all paragraphs and images
    content = []
    chapter_images = []
    
    # Process all content elements in order
    for element in soup.find_all(['p', 'img']):
        if element.name == 'p':
            text = element.get_text().strip()
            if text and len(text) > 10:  # Skip very short paragraphs (likely artifacts)
                # Clean up the text
                text = re.sub(r'\s+', ' ', text)  # Replace multiple spaces with single space
                text = text.replace('\n', ' ').strip()
                content.append(text)
        
        elif element.name == 'img':
            # Handle image references
            src = element.get('src', '')
            alt = element.get('alt', '')
            
            # Try to match the image source to our extracted images
            matching_image_id = None
            for path, image_id in image_path_map.items():
                if src in path or path.endswith(src):
                    matching_image_id = image_id
                    break
            
            if matching_image_id:
                # Add image placeholder in content
                image_placeholder = f"[IMAGE: {alt or 'Image'} - ID: {matching_image_id}]"
                content.append(image_placeholder)
                chapter_images.append(matching_image_id)
    
    return content, chapter_images
