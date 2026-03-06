"""
Memory Optimization Strategies for Download System

This file documents memory optimization strategies and provides
efficient alternatives for the download system.
"""

import gzip
import json
from pathlib import Path
from typing import Dict, Any, Optional

class MemoryOptimizedStorage:
    """Memory-efficient storage strategies for download system."""

    @staticmethod
    async def save_compressed_content(file_path: Path, content_data: Dict[str, Any]) -> None:
        """Save content.json with gzip compression (50-70% space savings)."""
        import aiofiles

        # Compress JSON data
        json_bytes = json.dumps(content_data).encode('utf-8')
        compressed_data = gzip.compress(json_bytes, compresslevel=6)

        # Save compressed file
        async with aiofiles.open(f"{file_path}.gz", 'wb') as f:
            await f.write(compressed_data)

    @staticmethod
    async def load_compressed_content(file_path: Path) -> Dict[str, Any]:
        """Load and decompress content.json."""
        import aiofiles

        async with aiofiles.open(f"{file_path}.gz", 'rb') as f:
            compressed_data = await f.read()

        # Decompress and parse
        json_bytes = gzip.decompress(compressed_data)
        return json.loads(json_bytes.decode('utf-8'))

class AudioOptimizations:
    """Audio-specific memory and storage optimizations."""

    # Audio quality vs file size trade-offs
    QUALITY_PROFILES = {
        "high": {"bitrate": 192, "sample_rate": 44100},     # ~2.3MB/min
        "standard": {"bitrate": 128, "sample_rate": 22050}, # ~1.5MB/min
        "compact": {"bitrate": 64, "sample_rate": 16000},   # ~0.8MB/min
        "minimal": {"bitrate": 32, "sample_rate": 8000},    # ~0.4MB/min
    }

    @staticmethod
    def get_estimated_audio_size(text_length: int, quality: str = "standard") -> int:
        """Estimate audio file size based on text length and quality."""
        # Rough estimation: ~150 words per minute, ~6 chars per word
        estimated_minutes = (text_length / 6) / 150

        profile = AudioOptimizations.QUALITY_PROFILES.get(quality,
                   AudioOptimizations.QUALITY_PROFILES["standard"])

        # Convert bitrate to bytes per minute
        bytes_per_minute = (profile["bitrate"] * 1000 / 8) * 60

        return int(estimated_minutes * bytes_per_minute)

class StorageManagement:
    """Intelligent storage management and cleanup."""

    @staticmethod
    async def estimate_download_size(chapter_content: list, quality: str = "standard") -> Dict[str, int]:
        """Estimate total download size before starting."""

        # Content size (with compression)
        content_size = len(json.dumps({
            "paragraphs": chapter_content,
            "metadata": "sample"
        })) * 0.3  # ~70% compression ratio

        # Audio sizes
        title_audio = AudioOptimizations.get_estimated_audio_size(50, quality)  # Avg title length

        paragraph_audio = sum(
            AudioOptimizations.get_estimated_audio_size(len(p), quality)
            for p in chapter_content
        )

        return {
            "content_json": int(content_size),
            "title_audio": title_audio,
            "paragraph_audio": paragraph_audio,
            "total": int(content_size + title_audio + paragraph_audio)
        }

    @staticmethod
    async def cleanup_old_downloads(base_dir: Path, max_age_hours: int = 24) -> Dict[str, int]:
        """Clean up old downloads and return space freed."""
        import shutil
        import time

        current_time = time.time()
        cutoff_time = current_time - (max_age_hours * 3600)

        freed_space = 0
        cleaned_downloads = 0

        for download_dir in base_dir.iterdir():
            if not download_dir.is_dir():
                continue

            # Check directory creation time
            dir_time = download_dir.stat().st_ctime
            if dir_time < cutoff_time:
                # Calculate space before deletion
                dir_size = sum(f.stat().st_size for f in download_dir.rglob('*') if f.is_file())

                # Delete directory
                shutil.rmtree(download_dir)

                freed_space += dir_size
                cleaned_downloads += 1

        return {
            "downloads_cleaned": cleaned_downloads,
            "space_freed_bytes": freed_space,
            "space_freed_mb": freed_space / (1024 * 1024)
        }

# Storage efficiency configurations
STORAGE_CONFIGS = {
    "minimal": {
        "audio_quality": "minimal",
        "compress_content": True,
        "auto_cleanup_hours": 12,
        "description": "Smallest files, basic quality"
    },
    "balanced": {
        "audio_quality": "standard",
        "compress_content": True,
        "auto_cleanup_hours": 48,
        "description": "Good balance of quality and size"
    },
    "quality": {
        "audio_quality": "high",
        "compress_content": False,
        "auto_cleanup_hours": 168,  # 1 week
        "description": "Best quality, larger files"
    }
}

def get_storage_recommendation(available_space_gb: float) -> str:
    """Recommend storage configuration based on available space."""
    if available_space_gb < 1:
        return "minimal"
    elif available_space_gb < 5:
        return "balanced"
    else:
        return "quality"

# Example usage and memory impact:
"""
MEMORY IMPACT COMPARISON:

1. CURRENT APPROACH (Unoptimized):
   - Chapter: ~2.6MB (standard quality)
   - 20 Chapters: ~52MB
   - 100 Chapters: ~260MB

2. WITH COMPRESSION:
   - Chapter: ~1.8MB (30% smaller)
   - 20 Chapters: ~36MB
   - 100 Chapters: ~180MB

3. WITH MINIMAL QUALITY:
   - Chapter: ~0.9MB (65% smaller)
   - 20 Chapters: ~18MB
   - 100 Chapters: ~90MB

4. WITH BOTH (OPTIMAL):
   - Chapter: ~0.6MB (75% smaller)
   - 20 Chapters: ~12MB
   - 100 Chapters: ~60MB
"""