"""Script to inspect EPUB structure using ebooklib."""
import sys
from ebooklib import epub
import ebooklib

epub_path = "/Users/akshatthegreat/Downloads/Projects/AudioBookPython/1_The_Way_of_Kings_B_Sanderson.epub"

book = epub.read_epub(epub_path)

print("=== EPUB Metadata ===")
print(f"Title: {book.get_metadata('DC', 'title')}")
print(f"Author: {book.get_metadata('DC', 'creator')}")

print("\n=== Table of Contents ===")
toc = book.toc
for i, item in enumerate(toc[:30]):  # First 30 TOC items
    if isinstance(item, tuple):
        section, children = item
        print(f"{i+1}. SECTION: {section.title}")
        for j, child in enumerate(children[:5]):
            if hasattr(child, 'title'):
                print(f"   {j+1}. {child.title} -> {child.href}")
    elif hasattr(item, 'title'):
        print(f"{i+1}. {item.title} -> {item.href}")

print(f"\n=== Document Items (first 30) ===")
items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
print(f"Total document items: {len(items)}")
for i, item in enumerate(sorted(items, key=lambda x: x.file_name)[:30]):
    print(f"{i+1}. {item.file_name}")
    # Get first heading from content
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(item.content, 'html.parser')
    h1 = soup.find('h1')
    h2 = soup.find('h2')
    h3 = soup.find('h3')
    heading = h1 or h2 or h3
    if heading:
        print(f"   First heading: {heading.get_text().strip()[:80]}")
    text_len = len(soup.get_text().strip())
    print(f"   Text length: {text_len}")
