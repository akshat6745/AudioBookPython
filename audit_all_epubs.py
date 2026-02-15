"""
Audit all EPUBs in the database for missing content.
Compares TOC-based word count vs Spine-based word count.
"""

import sys
import os
sys.path.insert(0, '/Users/akshatthegreat/Downloads/Projects/AudioBookPython')

from app.services.epub_parser import parse_epub_content as parse_toc
from app.services.epub_parser_spine import parse_epub_content_spine as parse_spine
from app.core.supabase_client import get_supabase_client

def calculate_word_count(chapters):
    """Calculate total word count from chapters."""
    return sum(sum(len(p.split()) for p in ch['content']) for ch in chapters)

def audit_epub(epub_path: str):
    """
    Audit a single EPUB file.
    Returns dict with results.
    """
    print(f"\n{'='*80}")
    print(f"AUDITING: {os.path.basename(epub_path)}")
    print('='*80)
    
    # Parse with both methods
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    try:
        novel_toc, chapters_toc, _ = parse_toc(epub_content)
        toc_words = calculate_word_count(chapters_toc)
        toc_chapters = len(chapters_toc)
    except Exception as e:
        print(f"❌ TOC parser failed: {e}")
        return None
    
    try:
        novel_spine, chapters_spine, _ = parse_spine(epub_content)
        spine_words = calculate_word_count(chapters_spine)
        spine_chapters = len(chapters_spine)
    except Exception as e:
        print(f"❌ Spine parser failed: {e}")
        return None
    
    print(f"\nTitle: {novel_toc.title}")
    print(f"Author: {novel_toc.author}")
    
    print(f"\nTOC Parser:   {toc_chapters} chapters, {toc_words:,} words")
    print(f"Spine Parser: {spine_chapters} chapters, {spine_words:,} words")
    
    # Calculate difference
    word_diff = spine_words - toc_words
    percent_diff = (word_diff / spine_words * 100) if spine_words > 0 else 0
    
    # Check database
    supabase = get_supabase_client()
    db_novel = supabase.table('novels').select('id, title, slug').ilike('title', f'%{novel_toc.title[:20]}%').execute()
    
    db_words = 0
    in_database = False
    if db_novel.data:
        in_database = True
        novel_id = db_novel.data[0]['id']
        db_chapters = supabase.table('chapters').select('word_count').eq('novel_id', novel_id).execute()
        db_words = sum(ch['word_count'] for ch in db_chapters.data)
        print(f"Database:     {len(db_chapters.data)} chapters, {db_words:,} words")
    else:
        print(f"Database:     ❌ Not found")
    
    # Determine status
    status = "✅ OK"
    issue = None
    
    if abs(word_diff) > 1000:  # More than 1000 word difference
        if percent_diff > 10:  # More than 10% difference
            status = "❌ MAJOR ISSUE"
            issue = f"TOC parser missing {word_diff:,} words ({percent_diff:.1f}% of content)"
        elif percent_diff > 5:
            status = "⚠️  WARNING"
            issue = f"TOC parser missing {word_diff:,} words ({percent_diff:.1f}% of content)"
    
    # Check if database matches
    if in_database:
        db_vs_spine_diff = abs(db_words - spine_words)
        if db_vs_spine_diff > 1000:
            db_percent_diff = (db_vs_spine_diff / spine_words * 100) if spine_words > 0 else 0
            if db_percent_diff > 5:
                status = "❌ DATABASE INCOMPLETE"
                issue = f"Database missing {db_vs_spine_diff:,} words ({db_percent_diff:.1f}%)"
    
    print(f"\nStatus: {status}")
    if issue:
        print(f"Issue: {issue}")
    
    return {
        'filename': os.path.basename(epub_path),
        'title': novel_toc.title,
        'toc_words': toc_words,
        'toc_chapters': toc_chapters,
        'spine_words': spine_words,
        'spine_chapters': spine_chapters,
        'db_words': db_words if in_database else None,
        'in_database': in_database,
        'status': status,
        'issue': issue,
        'needs_reupload': status.startswith('❌')
    }

def main():
    print("="*80)
    print("EPUB CONTENT AUDIT")
    print("="*80)
    
    # Find all EPUBs
    epub_files = []
    base_dir = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython"
    
    for file in os.listdir(base_dir):
        if file.endswith('.epub'):
            epub_files.append(os.path.join(base_dir, file))
    
    print(f"\nFound {len(epub_files)} EPUB files\n")
    
    results = []
    for epub_path in sorted(epub_files):
        result = audit_epub(epub_path)
        if result:
            results.append(result)
    
    # Summary
    print("\n" + "="*80)
    print("AUDIT SUMMARY")
    print("="*80)
    
    ok_count = sum(1 for r in results if r['status'] == "✅ OK")
    warning_count = sum(1 for r in results if r['status'].startswith("⚠️"))
    error_count = sum(1 for r in results if r['status'].startswith("❌"))
    
    print(f"\nTotal EPUBs audited: {len(results)}")
    print(f"  ✅ OK: {ok_count}")
    print(f"  ⚠️  Warnings: {warning_count}")
    print(f"  ❌ Issues: {error_count}")
    
    if error_count > 0:
        print(f"\n{'='*80}")
        print("EPUBs NEEDING RE-UPLOAD:")
        print('='*80)
        for r in results:
            if r['needs_reupload']:
                print(f"\n📕 {r['filename']}")
                print(f"   Title: {r['title']}")
                print(f"   Issue: {r['issue']}")
                print(f"   TOC: {r['toc_words']:,} words | Spine: {r['spine_words']:,} words")
    
    if warning_count > 0:
        print(f"\n{'='*80}")
        print("WARNINGS:")
        print('='*80)
        for r in results:
            if r['status'].startswith("⚠️"):
                print(f"\n📙 {r['filename']}")
                print(f"   Title: {r['title']}")
                print(f"   Issue: {r['issue']}")

if __name__ == "__main__":
    main()
