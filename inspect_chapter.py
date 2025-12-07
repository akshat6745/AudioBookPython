import asyncio
from app.api.novels import fetch_chapter
import structlog

# Configure structlog to print to console
structlog.configure(
    processors=[structlog.processors.JSONRenderer()],
)

async def inspect_chapter():
    novel_name = "the-villain-wants-to-live"
    chapter_number = 362
    
    print(f"Fetching {novel_name} chapter {chapter_number}...")
    try:
        chapter = await fetch_chapter(chapter_number, novel_name)
        paragraphs = chapter.get("content", [])
        print(f"Total paragraphs: {len(paragraphs)}")
        
        if len(paragraphs) > 288:
            target_para = paragraphs[288]
            print(f"Paragraph 288 length: {len(target_para)}")
            print(f"Paragraph 288 content (first 100 chars): {target_para[:100]}...")
        else:
            print("Paragraph 288 not found.")
            
    except Exception as e:
        print(f"Error fetching chapter: {e}")

if __name__ == "__main__":
    asyncio.run(inspect_chapter())
