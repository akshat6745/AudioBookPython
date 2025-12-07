import asyncio
import aiohttp
import uuid
import time
import sys

async def poll_progress(session, progress_id, stop_event):
    print(f"Starting to poll progress for ID: {progress_id}")
    while not stop_event.is_set():
        try:
            async with session.get(f"http://127.0.0.1:8080/download/progress/{progress_id}") as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"Progress: {data}")
                    if data.get("status") in ["completed", "error"]:
                        return
                elif response.status == 404:
                    # Only print occasionally to avoid spam
                    pass 
        except Exception as e:
            pass
        
        await asyncio.sleep(1)

async def download_file(session, progress_id, stop_event):
    url = f"http://127.0.0.1:8080/download-chapter/the-villain-wants-to-live/362?voice=en-US-AvaMultilingualNeural&dialogue_voice=en-GB-RyanNeural&progress_id={progress_id}"
    print(f"Starting download: {url}")
    
    # Retry logic for connection
    for i in range(5):
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    print("Download started, receiving data...")
                    size = 0
                    async for chunk in response.content.iter_chunked(1024):
                        size += len(chunk)
                    print(f"Download finished. Total size: {size} bytes")
                    stop_event.set()
                    return
                else:
                    print(f"Download failed with status: {response.status}")
                    text = await response.text()
                    print(f"Error: {text}")
                    stop_event.set()
                    return
        except Exception as e:
            print(f"Connection attempt {i+1} failed: {e}")
            await asyncio.sleep(2)
    
    print("Failed to connect after retries.")
    stop_event.set()

async def main():
    # Wait for server to be ready
    print("Waiting for server to be ready...")
    await asyncio.sleep(5)
    
    progress_id = str(uuid.uuid4())
    stop_event = asyncio.Event()
    
    async with aiohttp.ClientSession() as session:
        # Start download and polling concurrently
        await asyncio.gather(
            download_file(session, progress_id, stop_event),
            poll_progress(session, progress_id, stop_event)
        )

if __name__ == "__main__":
    asyncio.run(main())
