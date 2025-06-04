from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.models.schemas import TTSRequest
import edge_tts
import io
import re

router = APIRouter()

@router.post("/tts")
async def text_to_speech_post(request: TTSRequest = None, text: str = None, voice: str = "en-US-ChristopherNeural"):
    return await process_tts_request(request, text, voice)

@router.get("/tts")
async def text_to_speech_get(text: str, voice: str = "en-US-ChristopherNeural"):
    return await process_tts_request(None, text, voice)

async def process_tts_request(request: TTSRequest = None, text: str = None, voice: str = "en-US-ChristopherNeural"):
    try:
        if request:
            text_to_convert = request.text
            voice_to_use = request.voice
        else:
            if not text:
                raise HTTPException(status_code=400, detail="Text parameter is required if not using JSON body")
            text_to_convert = text
            voice_to_use = voice
        text_to_convert = text_to_convert.replace("***", "Asterisk Asterisk Asterisk")
        text_to_convert = re.sub(
            r'\s*([^S]*?website on Google to access chapters of novels early and in the highest quality[^.]*\.)\s*',
            ' ',
            text_to_convert,
            flags=re.IGNORECASE
        )
        if not text_to_convert or text_to_convert.isspace():
            text_to_convert = ""
        communicate = edge_tts.Communicate(text_to_convert, voice_to_use)
        output = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                output.write(chunk["data"])
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="audio/mp3",
            headers={
                "Content-Disposition": "attachment; filename=speech.mp3",
                "Cache-Control": "no-cache"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error in text-to-speech conversion: {str(e)}") 