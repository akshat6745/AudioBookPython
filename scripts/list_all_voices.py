import asyncio
import edge_tts

async def list_voices():
    voices = await edge_tts.list_voices()
    print("Available en-US voices:")
    for v in voices:
        if v["ShortName"].startswith("en-US"):
            print(f"- {v['ShortName']} ({v['Gender']})")

if __name__ == "__main__":
    asyncio.run(list_voices())
