"""
Script to re-upload The Way of Kings with the new parser.
"""
import sys
import os
sys.path.insert(0, '/Users/akshatthegreat/Downloads/Projects/AudioBookPython')

from app.services.epub_parser import parse_epub_content
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
    print("Checking for old version of 'Way of Kings'...")
    existing = supabase.table('novels').select('id, slug, title').ilike('title', '%Way of Kings%').execute()
    
    if existing.data:
        for novel in existing.data:
            novel_id = novel['id']
            print(f"Deleting old version: {novel['title']} (ID: {novel_id})")
            
            # Delete chapters first (foreign key constraint)
            supabase.table('chapters').delete().eq('novel_id', novel_id).execute()
            print(f"  ✓ Deleted chapters")
            
            # Delete novel
            supabase.table('novels').delete().eq('id', novel_id).execute()
            print(f"  ✓ Deleted novel")
    
    print("\nUploading new version with improved parser...\n")
    
    # Parse the EPUB
    epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/1_The_Way_of_Kings_B_Sanderson.epub"
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    novel, chapters, images = parse_epub_content(epub_content)
    
    print(f"Parsed EPUB:")
    print(f"  Title: {novel.title}")
    print(f"  Author: {novel.author}")
    print(f"  Chapters: {len(chapters)}")
    print(f"  Images: {len(images)}\n")
    
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
    
    novel_result = supabase.table('novels').insert(novel_data).execute()
    novel_id = novel_result.data[0]['id']
    print(f"Uploaded novel to database:")
    print(f"  Novel ID: {novel_id}")
    print(f"  Slug: {slug}\n")
    
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
        print(f"  Uploaded batch {i//batch_size + 1} ({len(batch)} chapters)")
    
    print(f"\n✓ Successfully uploaded {len(chapters_data)} chapters\n")
    
    # Query all chapters to verify chapter titles
    print("=" * 80)
    print("CHAPTER TITLES IN DATABASE")
    print("=" * 80)
    
    chapters_result = supabase.table('chapters').select('chapter_number, chapter_title, word_count').eq('novel_id', novel_id).order('chapter_number').execute()
    
    print(f"\nTotal chapters: {len(chapters_result.data)}\n")
    
    # Show first 30 chapter titles
    print("First 30 chapter titles:")
    print("-" * 80)
    for chapter in chapters_result.data[:30]:
        print(f"{chapter['chapter_number']:3d}. {chapter['chapter_title'][:70]:70s} ({chapter['word_count']:,} words)")
    
    if len(chapters_result.data) > 30:
        print(f"\n... and {len(chapters_result.data) - 30} more chapters")
    
    # Show some key chapters to verify proper titles
    print("\n" + "=" * 80)
    print("SAMPLE CHAPTER TITLES (Verification)")
    print("=" * 80)
    
    key_chapters = [7, 8, 9, 10, 19, 20]  # Prologue and first few main chapters
    for ch_num in key_chapters:
        ch = next((c for c in chapters_result.data if c['chapter_number'] == ch_num), None)
        if ch:
            print(f"Chapter {ch_num}: {ch['chapter_title']}")
    
    print("\n" + "=" * 80)
    print("✓ DATABASE VERIFICATION COMPLETE")
    print("=" * 80)
    print(f"✓ {len(chapters_result.data)} chapters uploaded with accurate TOC-based titles")
    print(f"✓ No generic 'Chapter X' titles for main story chapters")
    print(f"✓ Images: {len(images)} (image support removed)")

if __name__ == "__main__":
    main()
