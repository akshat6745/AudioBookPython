"""
Deep analysis of A Knight of the Seven Kingdoms EPUB structure.
Check actual content length and structure.
"""

import sys
import os
sys.path.insert(0, '/Users/akshatthegreat/Downloads/Projects/AudioBookPython')

from app.services.epub_parser import parse_epub_content
from app.core.supabase_client import get_supabase_client
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

def analyze_epub_structure(epub_path: str):
    """Analyze the EPUB file structure in detail."""
    
    print("=" * 80)
    print("DEEP EPUB STRUCTURE ANALYSIS")
    print("=" * 80)
    print(f"\nEPUB: {os.path.basename(epub_path)}\n")
    
    # Open EPUB
    book = epub.read_epub(epub_path)
    
    # Get metadata
    title = book.get_metadata('DC', 'title')[0][0] if book.get_metadata('DC', 'title') else "Unknown"
    author = book.get_metadata('DC', 'creator')[0][0] if book.get_metadata('DC', 'creator') else "Unknown"
    
    print(f"Title: {title}")
    print(f"Author: {author}\n")
    
    # Analyze TOC
    print("=" * 80)
    print("TABLE OF CONTENTS STRUCTURE")
    print("=" * 80)
    
    def print_toc(toc, level=0):
        """Recursively print TOC structure."""
        for item in toc:
            if isinstance(item, tuple):
                # This is a section with children
                section, children = item
                print("  " * level + f"SECTION: {section.title}")
                print_toc(children, level + 1)
            elif isinstance(item, epub.Link):
                print("  " * level + f"→ {item.title} ({item.href})")
            elif isinstance(item, epub.Section):
                print("  " * level + f"SECTION: {item.title}")
    
    print_toc(book.toc)
    
    # Parse with our parser
    print("\n" + "=" * 80)
    print("OUR PARSER RESULTS")
    print("=" * 80)
    
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    novel, parsed_chapters, _ = parse_epub_content(epub_content)
    
    print(f"\nTotal chapters parsed: {len(parsed_chapters)}\n")
    print("Chapter breakdown:")
    print("-" * 80)
    
    total_words = 0
    for i, ch in enumerate(parsed_chapters, 1):
        word_count = sum(len(p.split()) for p in ch['content'])
        char_count = sum(len(p) for p in ch['content'])
        para_count = len(ch['content'])
        total_words += word_count
        
        print(f"{i:2d}. {ch['chapterTitle'][:50]:50s}")
        print(f"    Words: {word_count:,} | Chars: {char_count:,} | Paragraphs: {para_count}")
        
        # Flag suspiciously long chapters
        if word_count > 30000:
            print(f"    ⚠️  WARNING: Very long chapter (likely contains multiple actual chapters)")
        elif word_count > 15000:
            print(f"    ⚠️  WARNING: Long chapter (may contain sub-chapters)")
    
    print(f"\nTotal word count: {total_words:,}")
    
    # Check database
    print("\n" + "=" * 80)
    print("DATABASE CONTENT")
    print("=" * 80)
    
    supabase = get_supabase_client()
    db_novel = supabase.table('novels').select('id, title').ilike('title', '%Knight of the Seven Kingdoms%').execute()
    
    if db_novel.data:
        novel_id = db_novel.data[0]['id']
        db_chapters = supabase.table('chapters').select('chapter_number, chapter_title, word_count, content').eq('novel_id', novel_id).order('chapter_number').execute()
        
        print(f"\nDatabase chapters: {len(db_chapters.data)}")
        print("-" * 80)
        
        db_total_words = 0
        for ch in db_chapters.data:
            db_total_words += ch['word_count']
            para_count = len(ch['content'])
            print(f"{ch['chapter_number']:2d}. {ch['chapter_title'][:50]:50s}")
            print(f"    Words: {ch['word_count']:,} | Paragraphs: {para_count}")
            
            if ch['word_count'] > 30000:
                print(f"    ⚠️  WARNING: Very long chapter in DB")
        
        print(f"\nTotal word count in DB: {db_total_words:,}")
        
        # Compare
        print("\n" + "=" * 80)
        print("COMPARISON")
        print("=" * 80)
        print(f"\nParsed total words:  {total_words:,}")
        print(f"Database total words: {db_total_words:,}")
        
        word_diff = total_words - db_total_words
        if abs(word_diff) > 100:
            print(f"\n❌ Word count mismatch: {word_diff:,} words difference")
            if word_diff > 0:
                print(f"   Database is MISSING {word_diff:,} words")
            else:
                print(f"   Database has EXTRA {abs(word_diff):,} words")
        else:
            print("\n✅ Word counts match")
    
    # Analyze spine for additional insight
    print("\n" + "=" * 80)
    print("SPINE (READING ORDER) ANALYSIS")
    print("=" * 80)
    
    spine_items = []
    for item_id, _ in book.spine:
        item = book.get_item_with_id(item_id)
        if item and item.get_type() == ebooklib.ITEM_DOCUMENT:
            # Parse content to check size
            soup = BeautifulSoup(item.content, 'html.parser')
            text = soup.get_text()
            word_count = len(text.split())
            
            spine_items.append({
                'file': item.file_name,
                'words': word_count
            })
    
    print(f"\nTotal spine items: {len(spine_items)}")
    print("\nFiles with substantial content (>500 words):")
    for item in spine_items:
        if item['words'] > 500:
            print(f"  {item['file']:40s} {item['words']:,} words")

if __name__ == "__main__":
    analyze_epub_structure(
        "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/George_R_R_Martin_-_Tales_of_Dunk_and_Egg_01_-_03_-_A_Knight_of_the_Seven_Kingdoms.epub"
    )
