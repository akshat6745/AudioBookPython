"""
Verify database content completeness by comparing with fresh EPUB parse.
This will check if any content is missing from the database.
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

def verify_database_content(epub_path: str):
    """
    Parse EPUB and compare with database content.
    Reports any missing or different content.
    """
    
    print("=" * 80)
    print("DATABASE CONTENT VERIFICATION")
    print("=" * 80)
    print(f"\nEPUB: {os.path.basename(epub_path)}\n")
    
    # Parse the EPUB
    print("Parsing EPUB file...")
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    novel, parsed_chapters, images = parse_epub_content(epub_content)
    print(f"✓ Parsed {len(parsed_chapters)} chapters from EPUB\n")
    
    # Get database content
    print("Fetching content from database...")
    supabase = get_supabase_client()
    
    # Find the novel in database
    db_novel = supabase.table('novels').select('id, title, author, slug').ilike('title', '%Way of Kings%').execute()
    
    if not db_novel.data:
        print("✗ ERROR: Novel not found in database!")
        return
    
    novel_id = db_novel.data[0]['id']
    print(f"✓ Found novel in database:")
    print(f"  ID: {novel_id}")
    print(f"  Title: {db_novel.data[0]['title']}")
    print(f"  Slug: {db_novel.data[0]['slug']}\n")
    
    # Get all chapters from database
    db_chapters = supabase.table('chapters').select(
        'chapter_number, chapter_title, content, word_count'
    ).eq('novel_id', novel_id).order('chapter_number').execute()
    
    print(f"✓ Fetched {len(db_chapters.data)} chapters from database\n")
    
    # Compare counts
    print("=" * 80)
    print("CHAPTER COUNT COMPARISON")
    print("=" * 80)
    print(f"\nParsed from EPUB: {len(parsed_chapters)}")
    print(f"Stored in DB:     {len(db_chapters.data)}")
    
    if len(parsed_chapters) != len(db_chapters.data):
        diff = len(parsed_chapters) - len(db_chapters.data)
        if diff > 0:
            print(f"⚠ WARNING: {diff} chapters are MISSING from database!")
        else:
            print(f"⚠ WARNING: {abs(diff)} EXTRA chapters in database!")
    else:
        print("✓ Chapter counts match")
    
    # Compare each chapter
    print("\n" + "=" * 80)
    print("CHAPTER-BY-CHAPTER COMPARISON")
    print("=" * 80)
    
    issues = []
    perfect_matches = 0
    
    max_compare = max(len(parsed_chapters), len(db_chapters.data))
    
    for i in range(max_compare):
        if i >= len(parsed_chapters):
            issues.append({
                'chapter': i + 1,
                'issue': 'EXTRA_IN_DB',
                'title': db_chapters.data[i]['chapter_title']
            })
            continue
        
        if i >= len(db_chapters.data):
            issues.append({
                'chapter': i + 1,
                'issue': 'MISSING_FROM_DB',
                'title': parsed_chapters[i]['chapterTitle']
            })
            continue
        
        parsed_ch = parsed_chapters[i]
        db_ch = db_chapters.data[i]
        
        # Compare titles
        title_match = parsed_ch['chapterTitle'] == db_ch['chapter_title']
        
        # Compare content
        parsed_content = ' '.join(parsed_ch['content'])
        db_content = ' '.join(db_ch['content'])
        
        content_similarity = calculate_similarity(parsed_content, db_content)
        
        # Check for issues
        chapter_issues = []
        
        if not title_match:
            chapter_issues.append(f"Title mismatch: '{parsed_ch['chapterTitle']}' vs '{db_ch['chapter_title']}'")
        
        if content_similarity < 1.0:
            char_diff = abs(len(parsed_content) - len(db_content))
            para_diff = abs(len(parsed_ch['content']) - len(db_ch['content']))
            chapter_issues.append(f"Content differs: {content_similarity*100:.2f}% similar ({char_diff} char diff, {para_diff} para diff)")
        
        if chapter_issues:
            issues.append({
                'chapter': i + 1,
                'issue': 'CONTENT_DIFF',
                'title': parsed_ch['chapterTitle'],
                'details': chapter_issues,
                'similarity': content_similarity
            })
        else:
            perfect_matches += 1
    
    # Report results
    print(f"\nPerfect Matches: {perfect_matches}/{max_compare}")
    
    if issues:
        print(f"Issues Found: {len(issues)}\n")
        
        # Group by issue type
        missing_from_db = [i for i in issues if i['issue'] == 'MISSING_FROM_DB']
        extra_in_db = [i for i in issues if i['issue'] == 'EXTRA_IN_DB']
        content_diffs = [i for i in issues if i['issue'] == 'CONTENT_DIFF']
        
        if missing_from_db:
            print("-" * 80)
            print(f"CHAPTERS MISSING FROM DATABASE ({len(missing_from_db)}):")
            print("-" * 80)
            for issue in missing_from_db:
                print(f"  Chapter {issue['chapter']}: {issue['title']}")
        
        if extra_in_db:
            print("\n" + "-" * 80)
            print(f"EXTRA CHAPTERS IN DATABASE ({len(extra_in_db)}):")
            print("-" * 80)
            for issue in extra_in_db:
                print(f"  Chapter {issue['chapter']}: {issue['title']}")
        
        if content_diffs:
            print("\n" + "-" * 80)
            print(f"CHAPTERS WITH CONTENT DIFFERENCES ({len(content_diffs)}):")
            print("-" * 80)
            for issue in content_diffs[:20]:  # Show first 20
                print(f"\nChapter {issue['chapter']}: {issue['title']}")
                print(f"  Similarity: {issue['similarity']*100:.2f}%")
                for detail in issue['details']:
                    print(f"  - {detail}")
            
            if len(content_diffs) > 20:
                print(f"\n  ... and {len(content_diffs) - 20} more chapters with differences")
    else:
        print("✓ No issues found - all content matches perfectly!")
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    
    if not issues:
        print("\n✓ DATABASE CONTENT IS COMPLETE AND ACCURATE")
        print("  All chapters match the parsed EPUB content perfectly.")
    else:
        print(f"\n⚠ FOUND {len(issues)} ISSUES:")
        if missing_from_db:
            print(f"  - {len(missing_from_db)} chapters missing from database")
        if extra_in_db:
            print(f"  - {len(extra_in_db)} extra chapters in database")
        if content_diffs:
            print(f"  - {len(content_diffs)} chapters with content differences")
        
        # Calculate what percentage is affected
        affected_pct = (len(issues) / max_compare) * 100
        if affected_pct < 5:
            print(f"\n  Impact: LOW ({affected_pct:.1f}% of chapters affected)")
        elif affected_pct < 20:
            print(f"\n  Impact: MEDIUM ({affected_pct:.1f}% of chapters affected)")
        else:
            print(f"\n  Impact: HIGH ({affected_pct:.1f}% of chapters affected)")
    
    print("\n" + "=" * 80)

if __name__ == "__main__":
    verify_database_content(
        "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/1_The_Way_of_Kings_B_Sanderson.epub"
    )
