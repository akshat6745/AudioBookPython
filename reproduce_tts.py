import asyncio
import edge_tts
from app.api.novels import fetch_chapter
from app.core.settings import settings
import aiohttp
import app.api.novels as novels_module

async def test_tts():
    # Setup session for fetch_chapter
    session = aiohttp.ClientSession()
    novels_module.session = session
    
    try:
        # Test Ryan voice
        voice = "en-US-RyanNeural"
        print(f"Testing voice: {voice}")
        communicate = edge_tts.Communicate("Hello, this is Ryan.", voice)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                print(f"Received audio chunk for {voice}")
                break
        
        # Fetch chapter
        print("Fetching chapter...")
        chapter = await fetch_chapter(362, "the-villain-wants-to-live")
        paragraphs = chapter.get("content", [])
        print(f"Fetched {len(paragraphs)} paragraphs")
        
        if paragraphs:
            first_para = paragraphs[0]
            print(f"First paragraph: {first_para[:50]}...")
            
            # Test TTS on first paragraph
            print("Testing TTS on first paragraph...")
            communicate = edge_tts.Communicate(first_para, "en-US-AvaMultilingualNeural")
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    print("Received audio chunk for paragraph")
                    break

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(test_tts())
