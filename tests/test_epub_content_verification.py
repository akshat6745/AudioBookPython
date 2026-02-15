"""
Automated test to verify uploaded EPUB content matches database.
This test should be run after uploading any EPUB to ensure data integrity.
"""

import pytest
import os
from app.services.epub_parser import parse_epub_content
from app.core.supabase_client import get_supabase_client
from difflib import SequenceMatcher


def calculate_similarity(text1: str, text2: str) -> float:
    """Calculate similarity ratio between two texts (0.0 to 1.0)."""
    return SequenceMatcher(None, text1, text2).ratio()


class TestEPUBContentVerification:
    """Test suite to verify EPUB content integrity after upload."""
    
    @pytest.fixture(scope="class")
    def supabase(self):
        """Get Supabase client."""
        return get_supabase_client()
    
    def verify_novel_content(self, epub_path: str, novel_title_pattern: str, supabase) -> dict:
        """
        Verify that EPUB content matches database content.
        Returns a dict with verification results.
        """
        # Parse EPUB
        with open(epub_path, 'rb') as f:
            epub_content = f.read()
        
        novel, parsed_chapters, _ = parse_epub_content(epub_content)
        
        # Find novel in database
        db_novel = supabase.table('novels').select('id, title, slug').ilike('title', novel_title_pattern).execute()
        
        if not db_novel.data:
            return {
                'success': False,
                'error': f'Novel not found in database (searched for: {novel_title_pattern})',
                'novel_found': False
            }
        
        novel_id = db_novel.data[0]['id']
        
        # Get chapters from database
        db_chapters = supabase.table('chapters').select(
            'chapter_number, chapter_title, content'
        ).eq('novel_id', novel_id).order('chapter_number').execute()
        
        # Compare
        issues = []
        
        # Check chapter count
        if len(parsed_chapters) != len(db_chapters.data):
            issues.append(f"Chapter count mismatch: {len(parsed_chapters)} parsed vs {len(db_chapters.data)} in DB")
        
        # Check each chapter
        for i in range(min(len(parsed_chapters), len(db_chapters.data))):
            parsed_ch = parsed_chapters[i]
            db_ch = db_chapters.data[i]
            
            # Check title
            if parsed_ch['chapterTitle'] != db_ch['chapter_title']:
                issues.append(f"Chapter {i+1} title mismatch: '{parsed_ch['chapterTitle']}' vs '{db_ch['chapter_title']}'")
            
            # Check content
            parsed_content = ' '.join(parsed_ch['content'])
            db_content = ' '.join(db_ch['content'])
            
            similarity = calculate_similarity(parsed_content, db_content)
            if similarity < 1.0:
                char_diff = abs(len(parsed_content) - len(db_content))
                issues.append(f"Chapter {i+1} content differs: {similarity*100:.2f}% similar ({char_diff} chars difference)")
        
        return {
            'success': len(issues) == 0,
            'novel_found': True,
            'novel_id': novel_id,
            'novel_title': db_novel.data[0]['title'],
            'parsed_chapters': len(parsed_chapters),
            'db_chapters': len(db_chapters.data),
            'issues': issues
        }
    
    def test_way_of_kings_content(self, supabase):
        """Verify The Way of Kings content in database."""
        epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/1_The_Way_of_Kings_B_Sanderson.epub"
        
        if not os.path.exists(epub_path):
            pytest.skip(f"EPUB file not found: {epub_path}")
        
        result = self.verify_novel_content(epub_path, '%Way of Kings%', supabase)
        
        assert result['novel_found'], result.get('error', 'Novel not found in database')
        assert result['success'], f"Verification failed with {len(result['issues'])} issues:\n" + "\n".join(result['issues'][:5])
        assert result['parsed_chapters'] == result['db_chapters'], "Chapter count mismatch"
    
    def test_unsouled_content(self, supabase):
        """Verify Unsouled content in database."""
        epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/Unsouled_-_Will_Wight.epub"
        
        if not os.path.exists(epub_path):
            pytest.skip(f"EPUB file not found: {epub_path}")
        
        result = self.verify_novel_content(epub_path, '%Unsouled%', supabase)
        
        # Only assert if the novel exists in database
        if not result['novel_found']:
            pytest.skip("Unsouled not found in database - skipping verification")
        
        assert result['success'], f"Verification failed with {len(result['issues'])} issues:\n" + "\n".join(result['issues'][:5])
        assert result['parsed_chapters'] == result['db_chapters'], "Chapter count mismatch"

    def test_words_of_radiance_content(self, supabase):
        """Verify Words of Radiance content in database."""
        epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/Words of Radiance (The Stormlight Archive, Book 2) by Brandon Sanderson.epub"
        
        if not os.path.exists(epub_path):
            pytest.skip(f"EPUB file not found: {epub_path}")
        
        # Use the title returned by the API
        result = self.verify_novel_content(epub_path, 'Words of Radiance (The Stormlight Archive, Book 2)', supabase)
        
        assert result['novel_found'], result.get('error', 'Novel not found in database')
        assert result['success'], f"Verification failed with {len(result['issues'])} issues:\n" + "\n".join(result['issues'][:5])
        assert result['parsed_chapters'] == result['db_chapters'], "Chapter count mismatch"

    def test_oathbringer_content(self, supabase):
        """Verify Oathbringer content in database."""
        epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/Oathbringer (The Stormlight Archive, Book 3) by Brandon Sanderson.epub"
        
        if not os.path.exists(epub_path):
            pytest.skip(f"EPUB file not found: {epub_path}")
        
        result = self.verify_novel_content(epub_path, 'Oathbringer (The Stormlight Archive, Book 3)', supabase)
        
        assert result['novel_found'], result.get('error', 'Novel not found in database')
        assert result['success'], f"Verification failed with {len(result['issues'])} issues:\n" + "\n".join(result['issues'][:5])
        assert result['parsed_chapters'] == result['db_chapters'], "Chapter count mismatch"


def verify_uploaded_epub(epub_path: str, novel_title_pattern: str) -> bool:
    """
    Helper function to verify a specific EPUB after upload.
    Can be called programmatically.
    
    Args:
        epub_path: Path to the EPUB file
        novel_title_pattern: Pattern to search for novel in database (e.g., '%Title%')
    
    Returns:
        True if verification passed, False otherwise
    """
    supabase = get_supabase_client()
    
    # Parse EPUB
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    novel, parsed_chapters, _ = parse_epub_content(epub_content)
    
    # Find novel in database
    db_novel = supabase.table('novels').select('id').ilike('title', novel_title_pattern).execute()
    
    if not db_novel.data:
        print(f"❌ Verification failed: Novel not found in database")
        return False
    
    novel_id = db_novel.data[0]['id']
    
    # Get chapters from database
    db_chapters = supabase.table('chapters').select(
        'chapter_number, chapter_title, content'
    ).eq('novel_id', novel_id).order('chapter_number').execute()
    
    # Compare
    if len(parsed_chapters) != len(db_chapters.data):
        print(f"❌ Chapter count mismatch: {len(parsed_chapters)} parsed vs {len(db_chapters.data)} in DB")
        return False
    
    # Quick content check on all chapters
    for i in range(len(parsed_chapters)):
        parsed_ch = parsed_chapters[i]
        db_ch = db_chapters.data[i]
        
        if parsed_ch['chapterTitle'] != db_ch['chapter_title']:
            print(f"❌ Chapter {i+1} title mismatch")
            return False
        
        parsed_content = ' '.join(parsed_ch['content'])
        db_content = ' '.join(db_ch['content'])
        
        if parsed_content != db_content:
            print(f"❌ Chapter {i+1} content mismatch")
            return False
    
    print(f"✅ Verification passed: All {len(parsed_chapters)} chapters match perfectly")
    return True
