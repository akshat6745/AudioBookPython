from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from app.api.novels import fetch_chapter
from app.api.tts import generate_audio, text_to_speech_dual_voice
from stream_zip import async_stream_zip, ZIP_32
import json
import structlog
import datetime
from typing import Dict, Any
import asyncio
import io

logger = structlog.get_logger()
router = APIRouter()

# In-memory store for progress. In production, use Redis.
progress_store: Dict[str, Dict[str, Any]] = {}

@router.get("/download/progress/{progress_id}")
async def get_download_progress(progress_id: str):
    """
    Get the progress of a download by its ID.
    """
    progress = progress_store.get(progress_id)
    if not progress:
        raise HTTPException(status_code=404, detail="Progress ID not found")
    return progress

@router.get("/download-chapter/{novel_name}/{chapter_number}")
async def download_chapter(
    novel_name: str, 
    chapter_number: int,
    voice: str = Query(..., description="Voice for narration"),
    dialogue_voice: str = Query(..., description="Voice for dialogue"),
    progress_id: str = Query(None, description="Unique ID to track progress")
):
    try:
        # Initialize progress if ID provided
        if progress_id:
            progress_store[progress_id] = {
                "status": "initializing",
                "total": 0,
                "current": 0,
                "percent": 0
            }

        # 1. Fetch chapter content
        logger.info("Fetching chapter content", novel=novel_name, chapter=chapter_number)
        chapter = await fetch_chapter(chapter_number, novel_name)
        paragraphs = chapter.get("content", [])
        chapter_title = chapter.get("chapterTitle", "Unknown Title")
        
        if not paragraphs:
            if progress_id:
                 progress_store[progress_id]["status"] = "error"
            raise HTTPException(status_code=404, detail="Chapter content not found")

        total_paragraphs = len(paragraphs)
        logger.info("Chapter fetched", paragraphs_count=total_paragraphs)

        # Update progress with total
        if progress_id:
            progress_store[progress_id].update({
                "status": "processing",
                "total": total_paragraphs,
                "current": 0,
                "percent": 0
            })

        async def generate_paragraph_audio(text, v, dv):
            buffer = io.BytesIO()
            async for chunk in text_to_speech_dual_voice(text, v, dv):
                buffer.write(chunk)
            return buffer.getvalue()

        async def member_generator():
            # 1. Add content.json
            content_data = {
                "novelName": novel_name,
                "chapterNumber": chapter_number,
                "chapterTitle": chapter_title,
                "paragraphs": paragraphs
            }
            
            async def content_stream():
                yield json.dumps(content_data, indent=2).encode("utf-8")

            yield (
                "content.json",
                datetime.datetime.now(),
                0o600,
                ZIP_32,
                content_stream()
            )

            # 2. Add Title Audio
            async def title_audio_stream():
                async for chunk in text_to_speech_dual_voice(chapter_title, voice, dialogue_voice):
                    yield chunk
            
            yield (
                "audio/title.mp3",
                datetime.datetime.now(),
                0o600,
                ZIP_32,
                title_audio_stream()
            )

            # 3. Add Paragraph Audios with Sliding Window Parallelism
            window_size = 15
            tasks = {}

            try:
                # Start initial batch
                for i in range(min(window_size, total_paragraphs)):
                    tasks[i] = asyncio.create_task(
                        generate_paragraph_audio(paragraphs[i], voice, dialogue_voice)
                    )

                for i in range(total_paragraphs):
                    # Schedule next task if needed
                    next_task_idx = i + window_size
                    if next_task_idx < total_paragraphs:
                        tasks[next_task_idx] = asyncio.create_task(
                            generate_paragraph_audio(paragraphs[next_task_idx], voice, dialogue_voice)
                        )
                    
                    logger.info("Processing paragraph", index=i, total=total_paragraphs)
                    
                    # Wait for current task with timeout
                    try:
                        # Dynamic timeout based on text length
                        # Base 30s + 0.2s per character. 
                        # Example: 1000 chars -> 30 + 200 = 230s
                        text_len = len(paragraphs[i])
                        timeout_seconds = 30.0 + (text_len * 0.2)
                        
                        audio_bytes = await asyncio.wait_for(tasks[i], timeout=timeout_seconds)
                        del tasks[i]
                    except asyncio.TimeoutError:
                        logger.error("Timeout generating audio for paragraph", index=i, length=len(paragraphs[i]), timeout=timeout_seconds)
                        raise HTTPException(status_code=504, detail=f"Timeout generating audio for paragraph {i+1} (length: {len(paragraphs[i])})")
                    except Exception as e:
                        logger.error("Error generating audio for paragraph", index=i, error=str(e))
                        raise e

                    # Update progress
                    if progress_id and progress_id in progress_store:
                        current_percent = round(((i + 1) / total_paragraphs) * 100, 1)
                        last_percent = progress_store[progress_id].get("percent", 0)
                        
                        # Update only if we crossed a 5% threshold or it's the first update
                        if current_percent - last_percent >= 5 or i == 0:
                            progress_store[progress_id]["current"] = i + 1
                            progress_store[progress_id]["percent"] = current_percent
                    
                    async def paragraph_audio_stream(data):
                        yield data
                    
                    yield (
                        f"audio/{i}.mp3",
                        datetime.datetime.now(),
                        0o600,
                        ZIP_32,
                        paragraph_audio_stream(audio_bytes)
                    )
                
                logger.info("All paragraphs processed")
                if progress_id and progress_id in progress_store:
                    progress_store[progress_id]["status"] = "completed"
                    progress_store[progress_id]["percent"] = 100

            finally:
                # Cleanup any pending tasks
                if tasks:
                    logger.info("Cleaning up pending tasks", count=len(tasks))
                    for task in tasks.values():
                        task.cancel()
                    await asyncio.gather(*tasks.values(), return_exceptions=True)

        # Return as streaming response
        return StreamingResponse(
            async_stream_zip(member_generator()),
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename={novel_name}_chapter_{chapter_number}.zip"
            }
        )

    except HTTPException:
        if progress_id and progress_id in progress_store:
            progress_store[progress_id]["status"] = "error"
        raise
    except Exception as e:
        logger.error("Error creating download package", error=str(e))
        if progress_id and progress_id in progress_store:
            progress_store[progress_id]["status"] = "error"
            progress_store[progress_id]["error"] = str(e)
        raise HTTPException(status_code=500, detail=f"Error creating download package: {str(e)}")
