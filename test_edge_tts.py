import asyncio
import edge_tts

async def main():
    """
    Test the edge-tts library directly
    """
    # Create a Communicate object
    communicate = edge_tts.Communicate("Hello, this is a test of the Edge TTS library.", "en-US-ChristopherNeural")
    
    # Save the audio to a file
    await communicate.save("test_direct.mp3")
    
    print("Audio saved to test_direct.mp3")

if __name__ == "__main__":
    asyncio.run(main())
