from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import StreamingResponse
from app.models.schemas import TTSDualVoiceRequest
import edge_tts
import io
import re
import asyncio
import structlog

logger = structlog.get_logger()

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


async def generate_audio(text: str, voice: str, max_retries: int = 3):
    """Generate audio with improved error handling and retry logic."""

    if not text or text.isspace():
        # For truly empty or whitespace-only text, generate minimal silence
        text = "."
    elif re.match(r'^\W*$', text):
        # For non-word characters like "...", "---", etc., convert to readable pause
        text = "pause"

    # Retry logic for TTS generation
    for attempt in range(max_retries):
        try:
            logger.info("Generating TTS audio", voice=voice, text_length=len(text), attempt=attempt + 1)

            communicate = edge_tts.Communicate(text, voice)

            has_audio = False
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    has_audio = True
                    yield chunk["data"]

            if has_audio:
                logger.info("TTS audio generated successfully", voice=voice, text_length=len(text))
                return
            else:
                logger.warning("No audio chunks received", voice=voice, text_length=len(text), attempt=attempt + 1)

        except edge_tts.exceptions.NoAudioReceived as e:
            logger.warning("NoAudioReceived exception", voice=voice, text_preview=text[:50], error=str(e), attempt=attempt + 1)
            if attempt == max_retries - 1:
                # Last attempt failed, return empty (no audio for this part)
                logger.error("Failed to generate audio after all retries", voice=voice, text_preview=text[:50])
                return

        except Exception as e:
            logger.error("TTS generation error", voice=voice, text_preview=text[:50], error=str(e), attempt=attempt + 1)
            if attempt == max_retries - 1:
                # Re-raise on final attempt
                raise HTTPException(status_code=500, detail=f"TTS generation failed after {max_retries} attempts: {str(e)}")

        # Wait before retry
        if attempt < max_retries - 1:
            await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
