import asyncio
import aiohttp
import os
from pathlib import Path

async def fetch_novel_with_tts(session, novel_name, chapter_number, voice="en-US-JennyNeural", dialogue_voice="en-US-GuyNeural"):
    """Fetch TTS audio for a specific chapter"""
    url = "http://localhost:8080/novel-with-tts"
    params = {
        "novelName": novel_name,
        "chapterNumber": chapter_number,
        "voice": voice,
        "dialogueVoice": dialogue_voice
    }
    
    try:
        async with session.get(url, params=params) as response:
            if response.status == 200:
                return await response.read()
            else:
                print(f"Error fetching chapter {chapter_number}: {response.status}")
                return None
    except Exception as e:
        print(f"Error fetching chapter {chapter_number}: {str(e)}")
        return None

async def main():
    novel_name = "lord-of-mysteries-2-circle-of-inevitability"
    start_chapter = 477
    end_chapter = 490
    
    # Create downloads folder if it doesn't exist
    downloads_folder = Path.home() / "Downloads" / "lord-of-mysteries-2-circle-of-inevitability-tts"
    downloads_folder.mkdir(parents=True, exist_ok=True)
    
    print(f"Downloading TTS audio for chapters {start_chapter}-{end_chapter} of {novel_name}")
    print(f"Saving to: {downloads_folder}")
    
    async with aiohttp.ClientSession() as session:
        # Process chapters sequentially to avoid overwhelming the server
        for chapter_num in range(start_chapter, end_chapter + 1):
            print(f"Fetching chapter {chapter_num}...")
            
            audio_data = await fetch_novel_with_tts(session, novel_name, chapter_num)
            
            if audio_data:
                # Save the audio file
                filename = f"chapter_{chapter_num:03d}.mp3"
                filepath = downloads_folder / filename
                
                with open(filepath, 'wb') as f:
                    f.write(audio_data)
                
                print(f"✓ Saved chapter {chapter_num} to {filename}")
            else:
                print(f"✗ Failed to fetch chapter {chapter_num}")
            
            # Small delay to be respectful to the server
            await asyncio.sleep(1)
    
    print(f"\nDownload complete! Files saved to: {downloads_folder}")

if __name__ == "__main__":
    asyncio.run(main())
