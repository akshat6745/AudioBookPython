import edge_tts
import asyncio

async def test_edge_tts():
    text = "Hello, this is a test."
    voice = "en-US-JennyNeural"
    communicate = edge_tts.Communicate(text, voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            print("Audio chunk received.")
        else:
            print(f"Chunk type: {chunk['type']}")

asyncio.run(test_edge_tts())
