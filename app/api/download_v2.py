from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from app.api.novels import fetch_chapter
from app.api.tts import text_to_speech_dual_voice
import json
import structlog
import datetime
import asyncio
import aiofiles
import os
import uuid
from typing import Dict, Any, Optional
from pydantic import BaseModel
from pathlib import Path

logger = structlog.get_logger()
router = APIRouter()

# Configuration
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# In-memory store for download progress
download_status: Dict[str, Dict[str, Any]] = {}

class DownloadRequest(BaseModel):
    novel_name: str
    chapter_number: int
    narrator_voice: str
    dialogue_voice: str

class DownloadResponse(BaseModel):
    download_id: str
    status: str
    message: str

class DownloadStatus(BaseModel):
    download_id: str
    status: str  # "pending", "processing", "completed", "error"
    progress: int  # 0-100
    total_files: int
    completed_files: int
    error_message: Optional[str] = None
    files: Optional[Dict[str, Any]] = None

@router.post("/download/chapter", response_model=DownloadResponse)
async def start_chapter_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    """Start downloading a chapter with all its audio files."""
    try:
        # Generate unique download ID
        download_id = str(uuid.uuid4())

        # Initialize download status
        download_status[download_id] = {
            "status": "pending",
            "progress": 0,
            "total_files": 0,
            "completed_files": 0,
            "error_message": None,
            "files": None,
            "created_at": datetime.datetime.now().isoformat()
        }

        # Start background download task
        background_tasks.add_task(
            process_chapter_download,
            download_id,
            request.novel_name,
            request.chapter_number,
            request.narrator_voice,
            request.dialogue_voice
        )

        logger.info("Download started", download_id=download_id, novel=request.novel_name, chapter=request.chapter_number)

        return DownloadResponse(
            download_id=download_id,
            status="pending",
            message="Download started successfully"
        )

    except Exception as e:
        logger.error("Error starting download", error=str(e))
        raise HTTPException(status_code=500, detail=f"Error starting download: {str(e)}")

@router.get("/download/status/{download_id}", response_model=DownloadStatus)
async def get_download_status(download_id: str):
    """Get the current status of a download."""
    if download_id not in download_status:
        raise HTTPException(status_code=404, detail="Download ID not found")

    status_data = download_status[download_id]

    return DownloadStatus(
        download_id=download_id,
        status=status_data["status"],
        progress=status_data["progress"],
        total_files=status_data["total_files"],
        completed_files=status_data["completed_files"],
        error_message=status_data.get("error_message"),
        files=status_data.get("files")
    )

@router.get("/download/file/{download_id}/{filename}")
async def download_file(download_id: str, filename: str):
    """Serve individual download files."""
    if download_id not in download_status:
        raise HTTPException(status_code=404, detail="Download ID not found")

    # Sanitize filename to prevent directory traversal
    safe_filename = filename.replace("..", "").replace("/", "").replace("\\", "")
    file_path = DOWNLOADS_DIR / download_id / safe_filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Determine media type based on file extension
    media_type = "application/json" if safe_filename.endswith('.json') else "audio/mpeg"

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=safe_filename
    )

async def process_chapter_download(
    download_id: str,
    novel_name: str,
    chapter_number: int,
    narrator_voice: str,
    dialogue_voice: str
):
    """Background task to process chapter download."""

    def update_status(**kwargs):
        """Update status dict only if the download hasn't been cleaned up."""
        if download_id not in download_status:
            return
        # Progress must never go backwards
        if "progress" in kwargs:
            kwargs["progress"] = max(
                kwargs["progress"],
                download_status[download_id].get("progress", 0),
            )
        download_status[download_id].update(kwargs)

    try:
        update_status(status="processing", progress=5)
        logger.info("Starting chapter processing", download_id=download_id)

        # Create download directory
        download_dir = DOWNLOADS_DIR / download_id
        download_dir.mkdir(exist_ok=True)

        # 1. Fetch chapter content
        logger.info("Fetching chapter content", novel=novel_name, chapter=chapter_number)
        chapter_data = await fetch_chapter(chapter_number, novel_name)
        paragraphs = chapter_data.get("content", [])
        chapter_title = chapter_data.get("chapterTitle", f"Chapter {chapter_number}")

        if not paragraphs:
            raise Exception("Chapter content not found")

        # Calculate total files: content.json + title.mp3 + paragraph mp3s
        total_files = 1 + 1 + len(paragraphs)  # content + title + paragraphs
        update_status(total_files=total_files, progress=10)

        # 2. Save content.json
        content_data = {
            "novel_name": novel_name,
            "chapter_number": chapter_number,
            "chapter_title": chapter_title,
            "paragraphs": paragraphs,
            "narrator_voice": narrator_voice,
            "dialogue_voice": dialogue_voice,
            "downloaded_at": datetime.datetime.now().isoformat()
        }

        content_file = download_dir / "content.json"
        async with aiofiles.open(content_file, 'w') as f:
            await f.write(json.dumps(content_data, indent=2))

        update_status(completed_files=1, progress=15)
        logger.info("Content saved", download_id=download_id)

        # 3 & 4. Generate title + all paragraph audio files concurrently
        logger.info("Generating audio (parallel)", download_id=download_id, paragraphs=len(paragraphs))

        from app.core.settings import settings
        MAX_CONCURRENT = settings.TTS_MAX_CONCURRENT
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)

        async def save_audio(filename: str, text: str) -> str:
            async with semaphore:
                file_path = download_dir / filename
                async with aiofiles.open(file_path, 'wb') as f:
                    async for chunk in text_to_speech_dual_voice(text, narrator_voice, dialogue_voice):
                        await f.write(chunk)
            return filename

        # Build all tasks: title + every paragraph
        audio_tasks = [asyncio.create_task(save_audio("title.mp3", chapter_title))]
        audio_tasks += [
            asyncio.create_task(save_audio(f"{i}.mp3", text))
            for i, text in enumerate(paragraphs)
        ]

        completed_count = 1  # content.json already done
        errors = []
        for coro in asyncio.as_completed(audio_tasks):
            try:
                filename = await coro
                completed_count += 1
                progress = int((completed_count / total_files) * 100)
                update_status(completed_files=completed_count, progress=progress)
                logger.info("Audio saved", download_id=download_id, file=filename, progress=progress)
            except Exception as e:
                errors.append(str(e))
                logger.error("Audio generation failed", download_id=download_id, error=str(e))

        if errors:
            raise Exception(f"Audio generation failed for {len(errors)} file(s): {errors[0]}")

        # 5. Create file manifest
        file_urls = {
            "content": f"/download/file/{download_id}/content.json",
            "audio": {
                "title": f"/download/file/{download_id}/title.mp3",
                "paragraphs": [f"/download/file/{download_id}/{i}.mp3" for i in range(len(paragraphs))]
            }
        }

        update_status(
            status="completed",
            progress=100,
            files=file_urls,
            completed_at=datetime.datetime.now().isoformat(),
        )
        logger.info("Download completed successfully", download_id=download_id, total_files=total_files)

    except Exception as e:
        error_msg = str(e)
        logger.error("Download failed", download_id=download_id, error=error_msg)
        update_status(
            status="error",
            error_message=error_msg,
            failed_at=datetime.datetime.now().isoformat(),
        )

@router.delete("/download/{download_id}")
async def cleanup_download(download_id: str):
    """Clean up download files and status."""
    if download_id not in download_status:
        raise HTTPException(status_code=404, detail="Download ID not found")

    try:
        # Remove files
        download_dir = DOWNLOADS_DIR / download_id
        if download_dir.exists():
            import shutil
            shutil.rmtree(download_dir)

        # Remove from status tracking
        del download_status[download_id]

        logger.info("Download cleaned up", download_id=download_id)
        return {"message": "Download cleaned up successfully"}

    except Exception as e:
        logger.error("Error cleaning up download", download_id=download_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Error cleaning up: {str(e)}")

@router.get("/download/cleanup/old")
async def cleanup_old_downloads(max_age_hours: int = 24):
    """Clean up downloads older than specified hours."""
    try:
        cutoff_time = datetime.datetime.now() - datetime.timedelta(hours=max_age_hours)
        cleaned_count = 0

        # Clean up status entries
        to_remove = []
        for download_id, status_data in download_status.items():
            created_at = datetime.datetime.fromisoformat(status_data.get("created_at", ""))
            if created_at < cutoff_time:
                to_remove.append(download_id)

        for download_id in to_remove:
            try:
                download_dir = DOWNLOADS_DIR / download_id
                if download_dir.exists():
                    import shutil
                    shutil.rmtree(download_dir)
                del download_status[download_id]
                cleaned_count += 1
            except Exception as e:
                logger.warning("Failed to clean up download", download_id=download_id, error=str(e))

        logger.info("Cleaned up old downloads", count=cleaned_count, max_age_hours=max_age_hours)
        return {"message": f"Cleaned up {cleaned_count} old downloads"}

    except Exception as e:
        logger.error("Error cleaning up old downloads", error=str(e))
        raise HTTPException(status_code=500, detail=f"Error cleaning up: {str(e)}")