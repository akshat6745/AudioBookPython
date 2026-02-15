"""Test script to analyze The Way of Kings EPUB structure and parsing."""
import sys
import os

# Add the project root to the path
sys.path.insert(0, '/Users/akshatthegreat/Downloads/Projects/AudioBookPython')

from app.services.epub_parser import parse_epub_content

def main():
    epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/1_The_Way_of_Kings_B_Sanderson.epub"
    
    print(f"Reading EPUB from: {epub_path}")
    if not os.path.exists(epub_path):
        print(f"Error: File not found at {epub_path}")
        return
    
    with open(epub_path, 'rb') as f:
        epub_content = f.read()
    
    print("Parsing EPUB content...")
    novel, firestore_chapters, images = parse_epub_content(epub_content)
    
    print(f"\n=== Novel Info ===")
    print(f"Title: {novel.title}")
    print(f"Author: {novel.author}")
    print(f"Total Chapters: {len(firestore_chapters)}")
    print(f"Total Images: {len(images)}")
    
    print(f"\n=== First 20 Chapter Titles ===")
    for i, chapter in enumerate(firestore_chapters[:20]):
        content_len = len("".join(chapter["content"]))
        print(f"{chapter['chapterNumber']:3d}. {chapter['chapterTitle'][:80]:80s} ({content_len:6d} chars)")
    
    # Save output to file
    output_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/wok_parsed.txt"
    with open(output_path, "w") as f:
        f.write(f"Title: {novel.title}\n")
        f.write(f"Author: {novel.author}\n")
        f.write(f"Total Chapters: {len(firestore_chapters)}\n\n")
        
        for chapter in firestore_chapters:
            f.write(f"{'='*80}\n")
            f.write(f"Chapter {chapter['chapterNumber']}: {chapter['chapterTitle']}\n")
            f.write(f"{'='*80}\n")
            content = "\n".join(chapter["content"])
            f.write(content)
            f.write("\n\n")
    
    print(f"\nFull output saved to: {output_path}")

if __name__ == "__main__":
    main()
