from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import StreamingResponse
from app.models.schemas import TTSDualVoiceRequest
import edge_tts
import re
import asyncio
import time
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

async def collect_audio(text: str, voice: str) -> bytes:
    chunks = []
    async for chunk in generate_audio(text, voice):
        chunks.append(chunk)
    return b"".join(chunks)


async def text_to_speech_dual_voice(
    text: str, paragraph_voice: str = "en-US-ChristopherNeural", dialogue_voice: str = "en-US-JennyNeural"
):
    try:
        text = re.sub(r'\*\s*\*\s*\*', "Asterisk Asterisk Asterisk", text)
        text = re.sub(
            r'\s*([^S]*?website on Google to access chapters of novels early and in the highest quality[^.]*\.)\s*',
            ' ',
            text,
            flags=re.IGNORECASE
        )

        dialogue_pattern = r'[\u201c\u201d"](.*?)[\u201c\u201d"]'
        dialogue_matches = re.finditer(dialogue_pattern, text)

        segments = []
        last_index = 0

        for match in dialogue_matches:
            start, end = match.span()

            paragraph_text = text[last_index:start].strip()
            if paragraph_text:
                segments.append((paragraph_text, paragraph_voice))

            dialogue_text = match.group(1).strip()
            if dialogue_text:
                segments.append((dialogue_text, dialogue_voice))

            last_index = end

        remaining_paragraph = text[last_index:].strip()
        if remaining_paragraph:
            segments.append((remaining_paragraph, paragraph_voice))

        logger.info("tts_start", segments=len(segments), text_length=len(text))
        t0 = time.perf_counter()

        tasks = [collect_audio(seg_text, seg_voice) for seg_text, seg_voice in segments]
        results = await asyncio.gather(*tasks)

        # If a dialogue segment produced no audio, fall back to narrator voice
        fallback_tasks = []
        fallback_indices = []
        for i, ((seg_text, seg_voice), audio_bytes) in enumerate(zip(segments, results)):
            if not audio_bytes and seg_voice == dialogue_voice:
                logger.warning("dialogue_fallback", text_preview=seg_text[:50])
                fallback_tasks.append(collect_audio(seg_text, paragraph_voice))
                fallback_indices.append(i)

        if fallback_tasks:
            fallback_results = await asyncio.gather(*fallback_tasks)
            results = list(results)
            for i, audio_bytes in zip(fallback_indices, fallback_results):
                results[i] = audio_bytes

        elapsed = time.perf_counter() - t0
        total_bytes = sum(len(r) for r in results)
        logger.info("tts_done", time_taken_s=round(elapsed, 2), segments=len(segments), total_bytes=total_bytes)

        for audio_bytes in results:
            if audio_bytes:
                yield audio_bytes

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error in dual-voice text-to-speech conversion: {str(e)}")


async def generate_audio(text: str, voice: str, max_retries: int = 3):
    if not text or text.isspace():
        text = "."
    elif re.match(r'^\W*$', text):
        text = "pause"

    for attempt in range(max_retries):
        try:
            t0 = time.perf_counter()
            communicate = edge_tts.Communicate(text, voice)

            has_audio = False
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    has_audio = True
                    yield chunk["data"]

            elapsed = time.perf_counter() - t0
            if has_audio:
                logger.info("segment_done", voice=voice, text_length=len(text), duration_s=round(elapsed, 2))
                return
            else:
                logger.warning("segment_no_audio", voice=voice, text_length=len(text), attempt=attempt + 1, duration_s=round(elapsed, 2))

        except edge_tts.exceptions.NoAudioReceived as e:
            logger.warning("segment_no_audio_received", voice=voice, text_preview=text[:50], error=str(e), attempt=attempt + 1)
            if attempt == max_retries - 1:
                logger.error("segment_failed", voice=voice, text_preview=text[:50])
                return

        except Exception as e:
            logger.error("segment_error", voice=voice, text_preview=text[:50], error=str(e), attempt=attempt + 1)
            if attempt == max_retries - 1:
                raise HTTPException(status_code=500, detail=f"TTS generation failed after {max_retries} attempts: {str(e)}")

        if attempt < max_retries - 1:
            await asyncio.sleep(0.5 * (attempt + 1))
