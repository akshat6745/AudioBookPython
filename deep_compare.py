"""
Deep dive content comparison - compare actual paragraphs from both parsers.
This will help us understand where the differences are coming from.
"""

import sys
import os
sys.path.insert(0, '/Users/akshatthegreat/Downloads/Projects/AudioBookPython')

from app.services.epub_parser import parse_epub_content as parse_toc
from app.services.epub_parser_spine import parse_epub_content_spine as parse_spine

def deep_compare_chapter(epub_path: str, chapter_num: int):
    """Deep comparison of a specific chapter."""
    
    print("=" * 80)
    print(f"DEEP DIVE: Chapter {chapter_num}")
    print("=" * 80)
    
    # Read EPUB file
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    # Parse with both methods
    novel_toc, chapters_toc, _ = parse_toc(epub_content)
    novel_spine, chapters_spine, _ = parse_spine(epub_content)
    
    idx = chapter_num - 1
    
    print(f"\nTOC-based Parser:")
    print(f"  Title: {chapters_toc[idx]['chapterTitle']}")
    print(f"  Paragraphs: {len(chapters_toc[idx]['content'])}")
    print(f"  Total chars: {sum(len(p) for p in chapters_toc[idx]['content'])}")
    if 'sourceFile' in chapters_toc[idx]:
        print(f"  Source: {chapters_toc[idx]['sourceFile']}")
    
    print(f"\nSpine-based Parser:")
    print(f"  Title: {chapters_spine[idx]['chapterTitle']}")
    print(f"  Paragraphs: {len(chapters_spine[idx]['content'])}")
    print(f"  Total chars: {sum(len(p) for p in chapters_spine[idx]['content'])}")
    if 'sourceFile' in chapters_spine[idx]:
        print(f"  Source: {chapters_spine[idx]['sourceFile']}")
    
    print(f"\n" + "-" * 80)
    print("First 10 paragraphs from TOC-based parser:")
    print("-" * 80)
    for i, para in enumerate(chapters_toc[idx]['content'][:10], 1):
        print(f"{i}. {para[:100]}")
    
    print(f"\n" + "-" * 80)
    print("First 10 paragraphs from Spine-based parser:")
    print("-" * 80)
    for i, para in enumerate(chapters_spine[idx]['content'][:10], 1):
        print(f"{i}. {para[:100]}")
    
    print(f"\n" + "=" * 80)
    print("ANALYSIS")
    print("=" * 80)
    
    # Check if first paragraphs match
    matches = 0
    for i in range(min(10, len(chapters_toc[idx]['content']), len(chapters_spine[idx]['content']))):
        if chapters_toc[idx]['content'][i] == chapters_spine[idx]['content'][i]:
            matches += 1
    
    print(f"\nFirst 10 paragraphs: {matches}/10 exact matches")
    
    # Check if TOC content is subset of spine content
    toc_text = ' '.join(chapters_toc[idx]['content'])
    spine_text = ' '.join(chapters_spine[idx]['content'])
    
    if toc_text in spine_text:
        print("✓ TOC content is a SUBSET of spine content")
        print("  (Spine parser may be extracting MORE content)")
    elif spine_text in toc_text:
        print("✓ Spine content is a SUBSET of TOC content")
        print("  (TOC parser may be extracting MORE content)")
    else:
        print("✗ Contents are DIFFERENT (not subsets of each other)")
        
        # Check overlap
        toc_words = set(toc_text.split())
        spine_words = set(spine_text.split())
        overlap = len(toc_words & spine_words)
        total = len(toc_words | spine_words)
        print(f"  Word overlap: {overlap}/{total} ({100*overlap/total:.1f}%)")

if __name__ == "__main__":
    # Check Chapter 7 (Prologue: To Kill) - this should be a main chapter
    print("\nChecking Chapter 7 (should be 'Prologue: To Kill'):\n")
    deep_compare_chapter(
        "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/1_The_Way_of_Kings_B_Sanderson.epub",
        7
    )
    
    print("\n\n" + "=" * 80)
    print("\nChecking Chapter 8 (should be '1: STORMBLESSED'):\n")
    deep_compare_chapter(
        "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/1_The_Way_of_Kings_B_Sanderson.epub",
        8
    )
