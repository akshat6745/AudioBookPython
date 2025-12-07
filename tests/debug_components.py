import asyncio
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.novels import fetch_chapter
import app.api.novels as novels_module
from app.api.tts import text_to_speech_dual_voice
import aiohttp

async def test_fetch():
    print("Testing fetch_chapter...")
    async with aiohttp.ClientSession() as session:
        novels_module.session = session
        try:
            chapter = await fetch_chapter(2720, "shadow-slave")
            print(f"SUCCESS: Fetched chapter. Title: {chapter.get('chapterTitle')}")
            print(f"Paragraphs: {len(chapter.get('content', []))}")
            return chapter
        except Exception as e:
            print(f"FAIL: fetch_chapter error: {e}")
            import traceback
            traceback.print_exc()
            return None

async def test_tts():
    print("Testing TTS...")
    try:
        text = "This is a test paragraph for verifying audio generation."
        voice = "en-US-ChristopherNeural"
        dialogue_voice = "en-US-JennyNeural"
        
        size = 0
        async for chunk in text_to_speech_dual_voice(text, voice, dialogue_voice):
            size += len(chunk)
            
        print(f"SUCCESS: Generated audio. Size: {size} bytes")
    except Exception as e:
        print(f"FAIL: TTS error: {e}")

async def main():
    chapter = await test_fetch()
    if chapter:
        await test_tts()
        
        # Test TTS on first paragraph of chapter
        first_para = chapter['content'][0]
        print(f"Testing TTS on first paragraph ({len(first_para)} chars)...")
        try:
            size = 0
            async for chunk in text_to_speech_dual_voice(first_para, "en-US-ChristopherNeural", "en-US-JennyNeural"):
                size += len(chunk)
            print(f"SUCCESS: First paragraph audio generated. Size: {size} bytes")
        except Exception as e:
             print(f"FAIL: First paragraph TTS error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"CRITICAL FAIL: {e}")
