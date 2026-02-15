import pytest
from bs4 import BeautifulSoup
from app.services.epub_parser import validate_parsed_content, run_parsing_strategy
from unittest.mock import MagicMock

def test_validation_empty():
    is_valid, score = validate_parsed_content([])
    assert is_valid is False
    assert score == 0.0

def test_validation_too_short():
    chapters = [
        {"chapterNumber": 1, "chapterTitle": "Chapter 1", "content": ["Short text."]}
    ]
    is_valid, score = validate_parsed_content(chapters)
    assert is_valid is False # Should be too short

def test_validation_valid():
    chapters = [
        {"chapterNumber": 1, "chapterTitle": "Chapter 1", "content": ["This is a much longer paragraph that should pass validation because it has enough characters to look like a real chapter. " * 10]}
    ]
    is_valid, score = validate_parsed_content(chapters)
    assert is_valid is True

def test_validation_toc_detection():
    # Many short chapters look like a TOC
    chapters = [
        {"chapterNumber": i, "chapterTitle": f"Link {i}", "content": [f"Page {i}"]}
        for i in range(10)
    ]
    is_valid, score = validate_parsed_content(chapters)
    assert is_valid is False

def test_strategy_fallback(mocker):
    # Mock ebooklib book and items
    mock_book = MagicMock()
    mock_item = MagicMock()
    mock_item.file_name = "chap1.xhtml"
    mock_item.content = b"<html><body><h1>Chapter 1</h1><p>Some content here that is long enough.</p></body></html>"
    mock_book.get_items_of_type.return_value = [mock_item]
    mock_book.get_metadata.return_value = [["Title"]]
    
    # Mock the strategies to return different results
    # Strategy 1 (STRICT) returns empty (fails validation)
    # Strategy 2 (LENIENT) returns valid content
    
    # We'll mock run_parsing_strategy to simulate this
    m_run = mocker.patch("app.services.epub_parser.run_parsing_strategy")
    m_run.side_effect = [
        [], # STRICT fails
        [{"chapterNumber": 1, "chapterTitle": "Ch1", "content": ["Content" * 100], "images": []}], # LENIENT succeeds
        [{"chapterNumber": 1, "chapterTitle": "Ch1", "content": ["Content" * 100], "images": []}] # FILE_BASED (not reached if LENIENT succeeds)
    ]
    
    from app.services.epub_parser import parse_epub_content
    
    # Mock read_epub to avoid file system operations
    m_read = mocker.patch("ebooklib.epub.read_epub", return_value=mock_book)
    m_extract_images = mocker.patch("app.services.epub_parser.extract_images_from_epub", return_value=[])
    
    novel, chapters, images = parse_epub_content(b"fake data")
    
    # Assertions
    assert m_run.call_count == 2 # STRICT then LENIENT
    assert len(chapters) == 1
    assert chapters[0]["chapterTitle"] == "Ch1"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
