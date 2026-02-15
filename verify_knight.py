"""
Verify A Knight of the Seven Kingdoms EPUB content.
"""

import sys
import os
sys.path.insert(0, '/Users/akshatthegreat/Downloads/Projects/AudioBookPython')

from app.services.epub_parser import parse_epub_content
from app.core.supabase_client import get_supabase_client
from difflib import SequenceMatcher

def calculate_similarity(text1: str, text2: str) -> float:
    """Calculate similarity ratio between two texts (0.0 to 1.0)."""
    return SequenceMatcher(None, text1, text2).ratio()

def main():
    epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/George_R_R_Martin_-_Tales_of_Dunk_and_Egg_01_-_03_-_A_Knight_of_the_Seven_Kingdoms.epub"
    
    print("=" * 80)
    print("VERIFYING: A Knight of the Seven Kingdoms")
    print("=" * 80)
    
    # Parse the EPUB
    print("\nParsing EPUB file...")
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    novel, parsed_chapters, images = parse_epub_content(epub_content)
    print(f"✓ Parsed {len(parsed_chapters)} chapters from EPUB")
    print(f"  Title: {novel.title}")
    print(f"  Author: {novel.author}\n")
    
    # Show chapter titles
    print("Parsed chapter titles:")
    print("-" * 80)
    for i, ch in enumerate(parsed_chapters[:30], 1):
        print(f"{i:3d}. {ch['chapterTitle'][:70]}")
    if len(parsed_chapters) > 30:
        print(f"... and {len(parsed_chapters) - 30} more chapters")
    
    # Check database
    print("\n" + "=" * 80)
    print("CHECKING DATABASE")
    print("=" * 80)
    
    supabase = get_supabase_client()
    
    # Search for the novel
    db_novel = supabase.table('novels').select('id, title, author, slug').or_(
        'title.ilike.%Knight of the Seven Kingdoms%,title.ilike.%Tales of Dunk and Egg%'
    ).execute()
    
    if not db_novel.data:
        print("\n⚠️  Novel NOT found in database")
        print("\nThis novel needs to be uploaded to the database.")
        print("Would you like to upload it? (The script will do this automatically)")
        return False, epub_content, novel, parsed_chapters
    
    novel_id = db_novel.data[0]['id']
    print(f"\n✓ Found novel in database:")
    print(f"  ID: {novel_id}")
    print(f"  Title: {db_novel.data[0]['title']}")
    print(f"  Slug: {db_novel.data[0]['slug']}")
    
    # Get chapters from database
    db_chapters = supabase.table('chapters').select(
        'chapter_number, chapter_title, content, word_count'
    ).eq('novel_id', novel_id).order('chapter_number').execute()
    
    print(f"\n✓ Database has {len(db_chapters.data)} chapters")
    
    # Compare
    print("\n" + "=" * 80)
    print("CONTENT COMPARISON")
    print("=" * 80)
    
    print(f"\nParsed chapters: {len(parsed_chapters)}")
    print(f"Database chapters: {len(db_chapters.data)}")
    
    if len(parsed_chapters) != len(db_chapters.data):
        diff = len(parsed_chapters) - len(db_chapters.data)
        if diff > 0:
            print(f"❌ {diff} chapters MISSING from database!")
        else:
            print(f"⚠️  {abs(diff)} EXTRA chapters in database")
        return True, epub_content, novel, parsed_chapters, novel_id
    
    # Check content
    issues = []
    
    for i in range(len(parsed_chapters)):
        parsed_ch = parsed_chapters[i]
        db_ch = db_chapters.data[i]
        
        # Check title
        if parsed_ch['chapterTitle'] != db_ch['chapter_title']:
            issues.append(f"Chapter {i+1}: Title mismatch - '{parsed_ch['chapterTitle']}' vs '{db_ch['chapter_title']}'")
        
        # Check content
        parsed_content = ' '.join(parsed_ch['content'])
        db_content = ' '.join(db_ch['content'])
        
        if parsed_content != db_content:
            similarity = calculate_similarity(parsed_content, db_content)
            char_diff = abs(len(parsed_content) - len(db_content))
            issues.append(f"Chapter {i+1}: Content differs ({similarity*100:.1f}% similar, {char_diff} chars diff)")
    
    if issues:
        print(f"\n❌ Found {len(issues)} issues:")
        for issue in issues[:10]:
            print(f"  - {issue}")
        if len(issues) > 10:
            print(f"  ... and {len(issues) - 10} more issues")
        return True, epub_content, novel, parsed_chapters, novel_id
    else:
        print("\n✅ All content matches perfectly!")
        return False, None, None, None, None

if __name__ == "__main__":
    needs_fix, epub_content, novel, parsed_chapters, *rest = main()
    
    if needs_fix:
        print("\n" + "=" * 80)
        print("FIXING DATABASE")
        print("=" * 80)
        
        novel_id = rest[0] if rest else None
        
        if novel_id:
            print(f"\nRe-uploading novel with ID {novel_id}...")
            # Will be handled by separate upload script
        else:
            print("\nNovel needs to be uploaded for the first time...")
