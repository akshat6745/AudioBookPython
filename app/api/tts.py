from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import StreamingResponse
from app.models.schemas import TTSDualVoiceRequest, TTSRequest
import edge_tts
import io
import re

router = APIRouter()

@router.post("/tts-dual-voice")
async def text_to_speech_dual_voice_post(request: TTSDualVoiceRequest = Body(...)):
    # print(f"Received dual voice TTS request: {request}")
    return await text_to_speech_dual_voice(request.text, request.paragraphVoice, request.dialogueVoice)

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

        audio_segments = []
        last_index = 0

        for match in dialogue_matches:
            start, end = match.span()

            # Paragraph before dialogue
            paragraph_text = text[last_index:start].strip()
            if paragraph_text:
                paragraph_audio = await generate_audio(paragraph_text, paragraph_voice)
                audio_segments.append(paragraph_audio)

            # Dialogue
            dialogue_text = match.group(1).strip()
            dialogue_audio = await generate_audio(dialogue_text, dialogue_voice)
            audio_segments.append(dialogue_audio)

            last_index = end

        # Remaining paragraph after last dialogue
        remaining_paragraph = text[last_index:].strip()
        if remaining_paragraph:
            remaining_audio = await generate_audio(remaining_paragraph, paragraph_voice)
            audio_segments.append(remaining_audio)

        # If no audio segments were generated (e.g., empty or all non-speakable text),
        # generate a short silence to ensure we return valid audio
        if not audio_segments:
            silence_audio = await generate_audio(" ", paragraph_voice)
            audio_segments.append(silence_audio)

        # Combine audio segments
        combined_audio = io.BytesIO()
        for segment in audio_segments:
            combined_audio.write(segment.getvalue())

        combined_audio.seek(0)

        return StreamingResponse(
            combined_audio,
            media_type="audio/mp3",
            headers={
                "Content-Disposition": "attachment; filename=speech.mp3",
                "Cache-Control": "no-cache"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error in dual-voice text-to-speech conversion: {str(e)}")


async def generate_audio(text: str, voice: str):
    if not text or text.isspace():
        # For truly empty or whitespace-only text, generate minimal silence
        communicate = edge_tts.Communicate(".", voice)
        output = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                output.write(chunk["data"])
        output.seek(0)
        return output
    elif re.match(r'^\W*$', text):
        # For non-word characters like "...", "---", etc., convert to readable pause
        # Map common punctuation to spoken equivalents or pauses
        if re.match(r'^\.+$', text):  # Multiple dots
            pause_text = "pause"
        elif re.match(r'^[!@#\$%\^&\*\(\)_\+=\[\]\{\}\|\\:";\'<>,\?/~`]+$', text):
            pause_text = "pause"
        else:
            pause_text = "pause"
        
        communicate = edge_tts.Communicate(pause_text, voice)
        output = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                output.write(chunk["data"])
        output.seek(0)
        return output

    communicate = edge_tts.Communicate(text, voice)
    output = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            output.write(chunk["data"])
    output.seek(0)
    return output
