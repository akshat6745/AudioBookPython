"""
Re-upload A Knight of the Seven Kingdoms using spine-based extraction.
The TOC-based parser misses content because each novella is split across multiple files.
"""

import sys
import os
sys.path.insert(0, '/Users/akshatthegreat/Downloads/Projects/AudioBookPython')

from app.services.epub_parser_spine import parse_epub_content_spine
from app.core.supabase_client import get_supabase_client
import re

def generate_slug(title: str) -> str:
    """Generate a URL-friendly slug from a title."""
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    return slug[:200] if len(slug) > 200 else slug

def calculate_word_count(content: list) -> int:
    """Calculate total word count from a list of paragraphs."""
    return sum(len(p.split()) for p in content)

def main():
    supabase = get_supabase_client()
    
    # Delete old version
    print("=" * 80)
    print("RE-UPLOADING A KNIGHT OF THE SEVEN KINGDOMS")
    print("=" * 80)
    print("\nDeleting old version...")
    existing = supabase.table('novels').select('id, slug, title').ilike('title', '%Knight of the Seven Kingdoms%').execute()
    
    if existing.data:
        for novel in existing.data:
            novel_id = novel['id']
            print(f"  Deleting: {novel['title']} (ID: {novel_id})")
            
            # Delete chapters first
            supabase.table('chapters').delete().eq('novel_id', novel_id).execute()
            print(f"    ✓ Deleted chapters")
            
            # Delete novel
            supabase.table('novels').delete().eq('id', novel_id).execute()
            print(f"    ✓ Deleted novel")
    
    print("\nParsing EPUB with spine-based parser...")
    
    # Parse the EPUB with spine-based parser
    epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/George_R_R_Martin_-_Tales_of_Dunk_and_Egg_01_-_03_-_A_Knight_of_the_Seven_Kingdoms.epub"
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    novel, chapters, images = parse_epub_content_spine(epub_content)
    
    print(f"\n✓ Parsed EPUB:")
    print(f"  Title: {novel.title}")
    print(f"  Author: {novel.author}")
    print(f"  Chapters: {len(chapters)}")
    
    total_words = sum(calculate_word_count(ch['content']) for ch in chapters)
    print(f"  Total words: {total_words:,}")
    
    # Show chapter breakdown
    print("\nChapter breakdown:")
    print("-" * 80)
    for i, ch in enumerate(chapters[:20], 1):
        wc = calculate_word_count(ch['content'])
        print(f"{i:3d}. {ch['chapterTitle'][:50]:50s} {wc:,} words")
    if len(chapters) > 20:
        print(f"... and {len(chapters) - 20} more chapters")
    
    # Upload to database
    slug = generate_slug(novel.title)
    
    novel_data = {
        "slug": slug,
        "title": novel.title,
        "author": novel.author or "Unknown",
        "status": "uploaded",
        "description": None,
        "genres": None,
    }
    
    print(f"\nUploading to database...")
    novel_result = supabase.table('novels').insert(novel_data).execute()
    novel_id = novel_result.data[0]['id']
    print(f"  ✓ Created novel (ID: {novel_id}, slug: {slug})")
    
    # Upload chapters
    chapters_data = []
    for chapter in chapters:
        chapter_content = chapter.get("content", [])
        chapters_data.append({
            "novel_id": novel_id,
            "chapter_number": chapter["chapterNumber"],
            "chapter_title": chapter.get("chapterTitle", f"Chapter {chapter['chapterNumber']}"),
            "content": chapter_content,
            "word_count": calculate_word_count(chapter_content),
        })
    
    batch_size = 50
    for i in range(0, len(chapters_data), batch_size):
        batch = chapters_data[i:i+batch_size]
        supabase.table('chapters').insert(batch).execute()
        print(f"  ✓ Uploaded batch {i//batch_size + 1} ({len(batch)} chapters)")
    
    print(f"\n✅ Successfully uploaded {len(chapters_data)} chapters")
    print(f"   Total words in database: {sum(ch['word_count'] for ch in chapters_data):,}")
    
    # Verify
    print("\n" + "=" * 80)
    print("VERIFICATION")
    print("=" * 80)
    
    # Re-parse and compare
    novel2, chapters2, _ = parse_epub_content_spine(epub_content)
    
    db_chapters = supabase.table('chapters').select('chapter_number, chapter_title, content, word_count').eq('novel_id', novel_id).order('chapter_number').execute()
    
    print(f"\nParsed chapters: {len(chapters2)}")
    print(f"Database chapters: {len(db_chapters.data)}")
    
    parsed_words = sum(calculate_word_count(ch['content']) for ch in chapters2)
    db_words = sum(ch['word_count'] for ch in db_chapters.data)
    
    print(f"\nParsed words: {parsed_words:,}")
    print(f"Database words: {db_words:,}")
    
    if parsed_words == db_words and len(chapters2) == len(db_chapters.data):
        print("\n✅ Verification passed - all content uploaded successfully!")
    else:
        print(f"\n⚠️  Verification warning: {abs(parsed_words - db_words):,} word difference")

if __name__ == "__main__":
    main()
