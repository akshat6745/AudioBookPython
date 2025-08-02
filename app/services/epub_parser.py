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
    
    # First pass: identify real chapters and their order
    chapters_data = []
    for item in items:
        if not is_chapter_content(item.file_name, item.content):
            continue
            
        # Parse HTML content
        soup = BeautifulSoup(item.content, 'html.parser')
        chapter_title = extract_chapter_title(soup)
        
        # Try to extract chapter number from title or content
        chapter_num_match = re.search(r'Chapter\s+(\d+)', chapter_title, re.IGNORECASE)
        if chapter_num_match:
            num = int(chapter_num_match.group(1))
        else:
            # If not found in title, check the content directly
            soup_temp = BeautifulSoup(item.content, 'html.parser')
            content_text = soup_temp.get_text()[:300]  # Check first 300 chars
            content_match = re.search(r'Chapter\s+(\d+)', content_text, re.IGNORECASE)
            if content_match:
                num = int(content_match.group(1))
            else:
                # Special handling for prologue (assign as chapter 0)
                if 'prologue' in chapter_title.lower():
                    num = 0
                else:
                    num = None
            
        chapters_data.append({
            'item': item,
            'title': chapter_title,
            'extracted_num': num,
            'filename': item.file_name
        })
    
    # Sort chapters by their extracted number, then by filename
    # Handle prologue (num=0) and None values properly
    def sort_key(x):
        num = x['extracted_num']
        if num is None:
            return (float('inf'), x['filename'])
        return (num, x['filename'])
    
    chapters_data.sort(key=sort_key)
    
    # Second pass: process chapters in correct order
    for idx, chapter_data in enumerate(chapters_data, 1):
        item = chapter_data['item']
        soup = BeautifulSoup(item.content, 'html.parser')
        
        # Remove unwanted tags and their content
        for tag in soup(['script', 'style']):
            tag.decompose()
        
        # Extract chapter title
        chapter_title = extract_chapter_title(soup) or f"Chapter {chapter_number}"
        
        # Extract and clean chapter content, including image references
        content, chapter_images = extract_chapter_content_with_images(soup, images)
        if not content:  # Skip empty chapters
            continue
            
        # Create chapter objects
        chapter = Chapter(
            number=chapter_number,
            title=chapter_title,
            content=content
        )
        
        # Create Firestore chapter document
        firestore_chapter = {
            "chapterNumber": chapter_number,
            "chapterTitle": chapter_title,
            "content": content,
            "images": chapter_images  # Add image references to chapter
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
        if text_content.lower().count('chapter') > 12:  # Likely TOC
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
        has_chapter_number = bool(re.search(r'chapter\s+\d+', first_300_chars))
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
            r'chapter\s+[ivxlc]+',  # Roman numerals
            r'ch\.\s*\d+',          # Abbreviated
            r'part\s+\d+',          # Parts
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
    More robust to handle different EPUB formatting styles.
    """
    # Get all text content to work with
    text_content = soup.get_text().strip()
    
    # Try common heading tags first - but be more flexible
    for tag in ['h1', 'h2', 'h3', 'h4']:
        heading = soup.find(tag)
        if heading:
            title = heading.get_text().strip()
            # Clean up and validate the title
            if len(title) > 2 and title.lower() not in ['chapter', 'the', 'a', 'an']:
                # Clean up common artifacts
                title = re.sub(r'^Chapter\s*(\d+)\s*[-:]\s*', r'Chapter \1: ', title, flags=re.IGNORECASE)
                title = re.sub(r'^Chapter\s*(\d+)\s*$', r'Chapter \1', title, flags=re.IGNORECASE)
                return title
            
    # Look for chapter patterns in the beginning of the text content
    lines = [line.strip() for line in text_content.split('\n') if line.strip()]
    
    # Multiple strategies for finding chapter titles
    strategies = [
        extract_multiline_chapter_title,
        extract_single_line_chapter_title,
        extract_pattern_based_title,
        extract_numeric_title
    ]
    
    for strategy in strategies:
        title = strategy(lines)
        if title:
            return title
    
    # Final fallback: try finding chapter headings in paragraphs
    for p in soup.find_all('p'):
        text = p.get_text().strip()
        if re.match(r'^(chapter|prologue|epilogue|part|interlude)\s+', text, re.IGNORECASE):
            return text[:100]  # Limit length
            
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
