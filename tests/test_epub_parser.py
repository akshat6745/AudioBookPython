import sys
import os
from bs4 import BeautifulSoup
import pytest

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.epub_parser import (
    extract_content_from_elements, 
    is_chapter_content, 
    extract_chapter_title
)

def test_extract_basic_paragraphs():
    html = """
    <div>
        <p>First paragraph.</p>
        <p>Second paragraph.</p>
    </div>
    """
    soup = BeautifulSoup(html, 'html.parser')
    content, _ = extract_content_from_elements(soup.children, [])
    assert content == ["First paragraph.", "Second paragraph."]

def test_extract_short_dialogue():
    html = """
    <div>
        <p>"Hello," he said.</p>
        <p>"Hi," she replied.</p>
        <p>"Go!"</p>
    </div>
    """
    soup = BeautifulSoup(html, 'html.parser')
    content, _ = extract_content_from_elements(soup.children, [])
    # Verify that short dialogue is not filtered out
    assert '"Go!"' in content
    assert content == ['"Hello," he said.', '"Hi," she replied.', '"Go!"']

def test_nested_tags_and_spans():
    html = """
    <div>
        <p>This is <span>bolded</span> text.</p>
        <blockquote>
            <p>Nested quote.</p>
        </blockquote>
        <div>Direct text in div.</div>
    </div>
    """
    soup = BeautifulSoup(html, 'html.parser')
    content, _ = extract_content_from_elements(soup.children, [])
    assert "This is bolded text." in content
    assert "Nested quote." in content
    assert "Direct text in div." in content

def test_image_extraction():
    images = [{"id": "img1", "originalPath": "images/cover.jpg"}]
    html = """
    <div>
        <p>Text before image.</p>
        <img src="images/cover.jpg" alt="Cover" />
        <p>Text after image.</p>
    </div>
    """
    soup = BeautifulSoup(html, 'html.parser')
    content, chapter_images = extract_content_from_elements(soup.children, images)
    assert "[IMAGE: Cover - ID: img1]" in content
    assert chapter_images == ["img1"]

def test_recursive_block_tags():
    html = """
    <section>
        <article>
            <p>Deeply nested paragraph.</p>
        </article>
        <div>
            Another paragraph.
            <br/>
            Text after break.
        </div>
    </section>
    """
    soup = BeautifulSoup(html, 'html.parser')
    content, _ = extract_content_from_elements(soup.children, [])
    assert "Deeply nested paragraph." in content
    assert "Another paragraph." in content
    assert "Text after break." in content

def test_is_chapter_content():
    # Valid chapter
    valid_content = b"<html><body>" + b"<p>Long story content here... " * 50 + b"</body></html>"
    assert is_chapter_content("chapter1.html", valid_content) == True
    
    # TOC
    toc_content = b"<html><body>" + b"<h1>Table of Contents</h1>" + b"<p>Chapter 1</p>" * 20 + b"</body></html>"
    assert is_chapter_content("toc.html", toc_content) == False
    
    # Excluded filename
    assert is_chapter_content("cover.xhtml", valid_content) == False

def test_extract_chapter_title_h1():
    html = "<html><body><h1>Chapter One: The Beginning</h1><p>Content</p></body></html>"
    soup = BeautifulSoup(html, 'html.parser')
    assert extract_chapter_title(soup) == "Chapter One: The Beginning"

def test_extract_chapter_title_para_fallback():
    html = "<html><body><p>PROLOGUE</p><p>Content</p></body></html>"
    soup = BeautifulSoup(html, 'html.parser')
    assert extract_chapter_title(soup) == "PROLOGUE"

if __name__ == "__main__":
    # Run tests if executed directly
    pytest.main([__file__])
