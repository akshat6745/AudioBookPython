import asyncio
import aiohttp
import sys

async def verify_health(session, base_url):
    try:
        async with session.get(f"{base_url}/health") as response:
            if response.status == 200:
                data = await response.json()
                print(f"✅ Health check passed: {data}")
                return True
            else:
                print(f"❌ Health check failed: {response.status}")
                return False
    except Exception as e:
        print(f"❌ Health check error: {e}")
        return False

async def verify_tts(session, base_url):
    try:
        # Test with a short text to avoid long generation times
        payload = {
            "text": "This is a production verification test.",
            "paragraphVoice": "en-US-ChristopherNeural",
            "dialogueVoice": "en-US-JennyNeural"
        }
        async with session.post(f"{base_url}/tts-dual-voice", json=payload) as response:
            if response.status == 200:
                content = await response.read()
                if len(content) > 0:
                    print(f"✅ TTS generation passed: Received {len(content)} bytes")
                    return True
                else:
                    print("❌ TTS generation failed: Empty response")
                    return False
            else:
                print(f"❌ TTS generation failed: {response.status}")
                return False
    except Exception as e:
        print(f"❌ TTS generation error: {e}")
        return False

async def main():
    base_url = "http://localhost:8080"
    print(f"Verifying API at {base_url}...")
    
    async with aiohttp.ClientSession() as session:
        health_ok = await verify_health(session, base_url)
        if not health_ok:
            print("⚠️ Skipping TTS test due to health check failure.")
            sys.exit(1)
            
        tts_ok = await verify_tts(session, base_url)
        
        if health_ok and tts_ok:
            print("\n🎉 All verification checks passed!")
            sys.exit(0)
        else:
            print("\n⚠️ Some checks failed.")
            sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
