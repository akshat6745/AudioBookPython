"""
Comparison script to validate EPUB parsers.
Compares TOC-based parser vs Spine-based parser to ensure content extraction accuracy.
"""

import sys
import os
sys.path.insert(0, '/Users/akshatthegreat/Downloads/Projects/AudioBookPython')

from app.services.epub_parser import parse_epub_content as parse_toc
from app.services.epub_parser_spine import parse_epub_content_spine as parse_spine
from difflib import SequenceMatcher
import json

def calculate_similarity(text1: str, text2: str) -> float:
    """Calculate similarity ratio between two texts (0.0 to 1.0)."""
    return SequenceMatcher(None, text1, text2).ratio()

def compare_content_lists(content1: list, content2: list) -> dict:
    """Compare two content lists (list of paragraphs)."""
    text1 = ' '.join(content1)
    text2 = ' '.join(content2)
    
    similarity = calculate_similarity(text1, text2)
    
    return {
        'paragraphs': {
            'parser1': len(content1),
            'parser2': len(content2),
            'difference': abs(len(content1) - len(content2))
        },
        'characters': {
            'parser1': len(text1),
            'parser2': len(text2),
            'difference': abs(len(text1) - len(text2))
        },
        'similarity': similarity,
        'match': similarity > 0.95  # 95% similarity threshold
    }

def compare_parsers(epub_path: str, output_file: str = None):
    """Compare TOC-based and Spine-based parsers on the same EPUB."""
    
    print("=" * 80)
    print("EPUB PARSER COMPARISON")
    print("=" * 80)
    print(f"\nEPUB File: {os.path.basename(epub_path)}\n")
    
    # Read EPUB file
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    # Parse with both methods
    print("Parsing with TOC-based parser...")
    novel_toc, chapters_toc, images_toc = parse_toc(epub_content)
    
    print("Parsing with Spine-based parser...")
    novel_spine, chapters_spine, images_spine = parse_spine(epub_content)
    
    print("\n" + "=" * 80)
    print("METADATA COMPARISON")
    print("=" * 80)
    
    print(f"\nTitle:")
    print(f"  TOC-based:   {novel_toc.title}")
    print(f"  Spine-based: {novel_spine.title}")
    print(f"  Match: {'✓' if novel_toc.title == novel_spine.title else '✗'}")
    
    print(f"\nAuthor:")
    print(f"  TOC-based:   {novel_toc.author}")
    print(f"  Spine-based: {novel_spine.author}")
    print(f"  Match: {'✓' if novel_toc.author == novel_spine.author else '✗'}")
    
    print(f"\nChapter Count:")
    print(f"  TOC-based:   {len(chapters_toc)} chapters")
    print(f"  Spine-based: {len(chapters_spine)} chapters")
    print(f"  Difference:  {abs(len(chapters_toc) - len(chapters_spine))} chapters")
    
    print("\n" + "=" * 80)
    print("CHAPTER TITLE COMPARISON")
    print("=" * 80)
    
    # Compare first 20 chapter titles
    max_compare = min(20, len(chapters_toc), len(chapters_spine))
    print(f"\nFirst {max_compare} chapters:")
    print("-" * 80)
    print(f"{'#':<4} {'TOC-based Title':<35} {'Spine-based Title':<35} {'Match':<5}")
    print("-" * 80)
    
    title_matches = 0
    for i in range(max_compare):
        toc_title = chapters_toc[i]['chapterTitle'][:33]
        spine_title = chapters_spine[i]['chapterTitle'][:33]
        match = '✓' if chapters_toc[i]['chapterTitle'] == chapters_spine[i]['chapterTitle'] else '✗'
        if match == '✓':
            title_matches += 1
        print(f"{i+1:<4} {toc_title:<35} {spine_title:<35} {match:<5}")
    
    print(f"\nTitle Match Rate: {title_matches}/{max_compare} ({100*title_matches/max_compare:.1f}%)")
    
    print("\n" + "=" * 80)
    print("CONTENT COMPARISON")
    print("=" * 80)
    
    # Compare content of matching chapters
    content_comparisons = []
    
    # Sample chapters to compare (first 5, middle 3, last 2)
    sample_indices = []
    if len(chapters_toc) > 0 and len(chapters_spine) > 0:
        max_idx = min(len(chapters_toc), len(chapters_spine)) - 1
        sample_indices = list(range(min(5, max_idx + 1)))  # First 5
        if max_idx >= 10:
            middle = max_idx // 2
            sample_indices.extend([middle - 1, middle, middle + 1])  # Middle 3
        if max_idx >= 3:
            sample_indices.extend([max_idx - 1, max_idx])  # Last 2
        sample_indices = sorted(set(sample_indices))
    
    print(f"\nComparing content of {len(sample_indices)} sample chapters...\n")
    
    for idx in sample_indices:
        if idx < len(chapters_toc) and idx < len(chapters_spine):
            comparison = compare_content_lists(
                chapters_toc[idx]['content'],
                chapters_spine[idx]['content']
            )
            
            content_comparisons.append({
                'chapter': idx + 1,
                'toc_title': chapters_toc[idx]['chapterTitle'],
                'comparison': comparison
            })
            
            status = '✓' if comparison['match'] else '✗'
            print(f"Chapter {idx+1}: {chapters_toc[idx]['chapterTitle'][:50]}")
            print(f"  Similarity: {comparison['similarity']*100:.2f}% {status}")
            print(f"  Paragraphs: {comparison['paragraphs']['parser1']} vs {comparison['paragraphs']['parser2']}")
            print(f"  Characters: {comparison['characters']['parser1']:,} vs {comparison['characters']['parser2']:,}")
            print()
    
    # Summary statistics
    perfect_matches = sum(1 for c in content_comparisons if c['comparison']['match'])
    avg_similarity = sum(c['comparison']['similarity'] for c in content_comparisons) / len(content_comparisons) if content_comparisons else 0
    
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    
    print(f"\nChapter Count:")
    print(f"  TOC-based:   {len(chapters_toc)}")
    print(f"  Spine-based: {len(chapters_spine)}")
    print(f"  Difference:  {abs(len(chapters_toc) - len(chapters_spine))}")
    
    print(f"\nContent Validation ({len(content_comparisons)} samples):")
    print(f"  Perfect Matches: {perfect_matches}/{len(content_comparisons)} ({100*perfect_matches/len(content_comparisons):.1f}%)")
    print(f"  Average Similarity: {avg_similarity*100:.2f}%")
    
    if avg_similarity > 0.95:
        print(f"\n✓ VALIDATION PASSED: Content extraction is highly accurate (>95% similarity)")
    elif avg_similarity > 0.85:
        print(f"\n⚠ VALIDATION WARNING: Content extraction is mostly accurate (>85% similarity)")
    else:
        print(f"\n✗ VALIDATION FAILED: Content extraction has significant differences (<85% similarity)")
    
    print("\n" + "=" * 80)
    
    # Write detailed output to file if requested
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("DETAILED COMPARISON RESULTS\n")
            f.write("=" * 80 + "\n\n")
            
            f.write("TOC-BASED PARSER CHAPTERS:\n")
            f.write("-" * 80 + "\n")
            for i, ch in enumerate(chapters_toc[:30], 1):
                f.write(f"{i}. {ch['chapterTitle']}\n")
            if len(chapters_toc) > 30:
                f.write(f"... and {len(chapters_toc) - 30} more\n")
            
            f.write("\n\nSPINE-BASED PARSER CHAPTERS:\n")
            f.write("-" * 80 + "\n")
            for i, ch in enumerate(chapters_spine[:30], 1):
                f.write(f"{i}. {ch['chapterTitle']}\n")
            if len(chapters_spine) > 30:
                f.write(f"... and {len(chapters_spine) - 30} more\n")
            
            f.write(f"\n\nContent comparisons saved: {len(content_comparisons)} chapters analyzed\n")
        
        print(f"Detailed results saved to: {output_file}")

if __name__ == "__main__":
    # Test with The Way of Kings
    print("\n")
    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 20 + "TESTING: The Way of Kings" + " " * 33 + "║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    compare_parsers(
        "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/1_The_Way_of_Kings_B_Sanderson.epub",
        "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/comparison_wok.txt"
    )
    
    print("\n\n")
    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 28 + "TESTING: Unsouled" + " " * 33 + "║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    compare_parsers(
        "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/Unsouled_-_Will_Wight.epub",
        "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/comparison_unsouled.txt"
    )
