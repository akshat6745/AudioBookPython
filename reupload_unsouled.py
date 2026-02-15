"""
Re-upload Unsouled with the new TOC-based parser.
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
    print("Checking for old version of 'Unsouled'...")
    existing = supabase.table('novels').select('id, slug, title').ilike('title', '%Unsouled%').execute()
    
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
    epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/Unsouled_-_Will_Wight.epub"
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
    
    # Show all chapter titles
    print("All chapter titles:")
    print("-" * 80)
    for chapter in chapters_result.data:
        print(f"{chapter['chapter_number']:3d}. {chapter['chapter_title'][:70]:70s} ({chapter['word_count']:,} words)")
    
    print("\n" + "=" * 80)
    print("✓ DATABASE UPLOAD COMPLETE")
    print("=" * 80)
    print(f"✓ {len(chapters_result.data)} chapters uploaded with accurate TOC-based titles")
    print(f"✓ Images: {len(images)} (image support removed)")
    
    # Run verification
    print("\n" + "=" * 80)
    print("RUNNING VERIFICATION")
    print("=" * 80)
    
    from tests.test_epub_content_verification import verify_uploaded_epub
    success = verify_uploaded_epub(epub_path, '%Unsouled%')
    
    if success:
        print("\n✅ VERIFICATION PASSED - All content matches perfectly!")
    else:
        print("\n⚠️ VERIFICATION FAILED - Please check logs above")

if __name__ == "__main__":
    main()
