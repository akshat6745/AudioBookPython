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
        
        # Try to extract chapter number from title
        chapter_num_match = re.search(r'Chapter\s+(\d+)', chapter_title, re.IGNORECASE)
        if chapter_num_match:
            num = int(chapter_num_match.group(1))
        else:
            num = None
            
        chapters_data.append({
            'item': item,
            'title': chapter_title,
            'extracted_num': num,
            'filename': item.file_name
        })
    
    # Sort chapters by their extracted number, then by filename
    chapters_data.sort(key=lambda x: (x['extracted_num'] if x['extracted_num'] else float('inf'), x['filename']))
    
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
    Check if the file is likely to be a chapter by analyzing both filename and content
    """
    file_name_lower = file_name.lower()
    
    # Always exclude certain patterns
    exclude_patterns = ['cover', 'toc', 'contents', 'copyright', 'title', 'thank', 'oceanof']
    if any(pattern in file_name_lower for pattern in exclude_patterns):
        return False
    
    try:
        # Check content size - if too small, likely not a chapter
        if len(content) < 500:  # Less than 500 bytes is probably not a chapter
            return False
            
        # Parse the content and check for typical chapter characteristics
        soup = BeautifulSoup(content, 'html.parser')
        text_content = soup.get_text().strip()
        
        # Skip if content is too short
        if len(text_content) < 200:  # Less than 200 characters is probably not a chapter
            return False
            
        # Look for chapter indicators in content
        chapter_indicators = ['chapter', 'prologue', 'epilogue', 'interlude']
        has_chapter_indicator = any(indicator in text_content.lower()[:100] for indicator in chapter_indicators)
        
        return has_chapter_indicator or bool(re.search(r'Chapter\s+\d+', text_content[:100]))
            
    except Exception:
        return False

def extract_chapter_title(soup: BeautifulSoup) -> str:
    """
    Extract chapter title from the HTML content
    """
    # Try common heading tags
    for tag in ['h1', 'h2', 'h3']:
        heading = soup.find(tag)
        if heading:
            title = heading.get_text().strip()
            # Clean up common artifacts
            title = re.sub(r'^Chapter\s*(\d+)\s*[-:]\s*', r'Chapter \1: ', title, flags=re.IGNORECASE)
            title = re.sub(r'^Chapter\s*(\d+)\s*$', r'Chapter \1', title, flags=re.IGNORECASE)
            return title
            
    # Try finding chapter headings in paragraphs
    for p in soup.find_all('p'):
        text = p.get_text().strip()
        if re.match(r'^(chapter|prologue|epilogue|interlude)\s*', text, re.IGNORECASE):
            return text
            
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
