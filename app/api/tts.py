from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import StreamingResponse
from app.models.schemas import TTSDualVoiceRequest
import edge_tts
import io
import re
import asyncio

router = APIRouter()

@router.post("/tts-dual-voice")
async def text_to_speech_dual_voice_post(request: TTSDualVoiceRequest = Body(...)):
    async def audio_generator():
        async for chunk in text_to_speech_dual_voice(request.text, request.paragraphVoice, request.dialogueVoice):
            yield chunk

    return StreamingResponse(
        audio_generator(),
        media_type="audio/mp3",
        headers={
            "Content-Disposition": "attachment; filename=speech.mp3",
            "Cache-Control": "no-cache"
        }
    )

async def text_to_speech_dual_voice(
    text: str, paragraph_voice: str = "en-US-ChristopherNeural", dialogue_voice: str = "en-US-JennyNeural"
):
    try:
        # Replace specific patterns and check for non-speaking characters
        text = re.sub(r'\*\s*\*\s*\*', "Asterisk Asterisk Asterisk", text)
        text = re.sub(
            r'\s*([^S]*?website on Google to access chapters of novels early and in the highest quality[^.]*\.)\s*',
            ' ',
            text,
            flags=re.IGNORECASE
        )

        # Split text into dialogue and paragraph parts
        dialogue_pattern = r'["“](.*?)["”]'
        dialogue_matches = re.finditer(dialogue_pattern, text)

        last_index = 0

        for match in dialogue_matches:
            start, end = match.span()

            # Paragraph before dialogue
            paragraph_text = text[last_index:start].strip()
            if paragraph_text:
                async for chunk in generate_audio(paragraph_text, paragraph_voice):
                    yield chunk

            # Dialogue
            dialogue_text = match.group(1).strip()
            async for chunk in generate_audio(dialogue_text, dialogue_voice):
                yield chunk

            last_index = end

        # Remaining paragraph after last dialogue
        remaining_paragraph = text[last_index:].strip()
        if remaining_paragraph:
            async for chunk in generate_audio(remaining_paragraph, paragraph_voice):
                yield chunk

    except Exception as e:
        # In a generator, we can't easily raise HTTP exceptions once streaming starts,
        # but we can log it. For now, re-raise to be caught by the caller if it hasn't started yielding.
        raise HTTPException(status_code=400, detail=f"Error in dual-voice text-to-speech conversion: {str(e)}")


async def generate_audio(text: str, voice: str):
    if not text or text.isspace():
        # For truly empty or whitespace-only text, generate minimal silence
        communicate = edge_tts.Communicate(".", voice)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]
        return
    elif re.match(r'^\W*$', text):
        # For non-word characters like "...", "---", etc., convert to readable pause
        pause_text = "pause"
        communicate = edge_tts.Communicate(pause_text, voice)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]
        return

    communicate = edge_tts.Communicate(text, voice)
    try:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]
    except edge_tts.exceptions.NoAudioReceived as e:
        print(f"Warning: No audio received for text: '{text[:50]}...' using voice {voice}. Error: {e}")
        # Yield a small silence (e.g. 0.5s) so the audio doesn't feel cut off? 
        # Or just return. For now, we return successfully (empty audio for this part).
        return
