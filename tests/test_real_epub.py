import sys
import os
import io

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.epub_parser import parse_epub_content

def test_real_epub(epub_path, output_path):
    print(f"Reading EPUB from: {epub_path}")
    if not os.path.exists(epub_path):
        print(f"Error: File not found at {epub_path}")
        return

    with open(epub_path, 'rb') as f:
        epub_content = f.read()

    print("Parsing EPUB content...")
    novel, firestore_chapters, images = parse_epub_content(epub_content)

    print(f"Parsed {len(firestore_chapters)} chapters.")
    print(f"Extracted {len(images)} images.")
    
    if images:
        print(f"Extracted {len(images)} images:")
        for img in images[:5]:
            print(f"- ID: {img['id']}, Path: {img['originalPath']}, Type: {img['contentType']}, Size: {img['size']}")
    
    with open(output_path, 'w', encoding='utf-8') as out:
        out.write(f"Title: {novel.title}\n")
        out.write(f"Author: {novel.author}\n")
        out.write(f"Chapters Count: {len(firestore_chapters)}\n")
        out.write("="*50 + "\n\n")

        for ch in firestore_chapters:
            out.write(f"CHAPTER {ch['chapterNumber']}: {ch['chapterTitle']}\n")
            out.write("-" * 30 + "\n")
            for p in ch['content']:
                out.write(p + "\n\n")
            out.write("\n" + "="*50 + "\n\n")

    print(f"Results saved to: {output_path}")

if __name__ == "__main__":
    epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/Unsouled_-_Will_Wight.epub"
    output_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/unsouled_parsed.txt"
    test_real_epub(epub_path, output_path)
