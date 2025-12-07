import asyncio
import edge_tts
from app.api.novels import fetch_chapter
import aiohttp
import app.api.novels as novels_module
from app.api.tts import text_to_speech_dual_voice

async def reproduce():
    session = aiohttp.ClientSession()
    novels_module.session = session
    
    try:
        novel_name = "shadow-slave"
        chapter_number = 4
        voice = "en-US-AvaMultilingualNeural"
        dialogue_voice = "en-US-RyanNeural"
        
        print(f"Fetching {novel_name} chapter {chapter_number}...")
        chapter = await fetch_chapter(chapter_number, novel_name)
        paragraphs = chapter.get("content", [])
        print(f"Fetched {len(paragraphs)} paragraphs.")
        
        for i, p in enumerate(paragraphs):
            # print(f"Processing paragraph {i}...")
            try:
                # we just iterate to trigger the generator
                async for chunk in text_to_speech_dual_voice(p, voice, dialogue_voice):
                    pass
            except Exception as e:
                print(f"FAILED at paragraph {i}:")
                print(f"Text content: '{p}'")
                print(f"Error: {e}")
                # We want to continue to see if others fail too, or maybe stop on first failure like original code
                # But for reproduction, seeing the text is most important.
                break
                
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(reproduce())
