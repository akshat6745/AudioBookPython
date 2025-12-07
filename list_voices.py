import asyncio
import edge_tts

async def list_voices():
    voices = await edge_tts.list_voices()
    for v in voices:
        if "Ryan" in v["ShortName"]:
            print(f"Found Ryan: {v['ShortName']} - {v['Gender']}")
        if "Ava" in v["ShortName"]:
            print(f"Found Ava: {v['ShortName']} - {v['Gender']}")

if __name__ == "__main__":
    asyncio.run(list_voices())
