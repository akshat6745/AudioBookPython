from fastapi import APIRouter, UploadFile, HTTPException
from fastapi.responses import Response
from app.models.schemas import NovelUploadResponse, ImageInfo, NovelImagesResponse
from app.services.epub_parser import parse_epub_content
from app.core.supabase_client import get_supabase_client   # still used for epub_images only
from app.core.d1_client import get_d1_client
from app.services.cloudflare_service import upload_chapter_text_to_r2
import re
import gzip
import base64
import structlog

router = APIRouter()
logger = structlog.get_logger()


def generate_slug(title: str) -> str:
    """Generate a URL-friendly slug from a title."""
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    return slug[:200] if len(slug) > 200 else slug


def calculate_word_count(content: list) -> int:
    return sum(len(p.split()) for p in content)


# ── EPUB Upload ───────────────────────────────────────────────────────────────

@router.post("/upload-epub", response_model=NovelUploadResponse)
async def upload_epub(file: UploadFile):
    """
    Upload and parse an EPUB file.
    Novel metadata → Cloudflare D1
    Chapter text content → Cloudflare R2 (gzip compressed)
    Images → Supabase (binary blobs, unchanged)
    """
    if not file.filename.endswith('.epub'):
        raise HTTPException(status_code=400, detail="File must be an EPUB file")

    try:
        content = await file.read()
        novel, chapters, images = parse_epub_content(content)

        d1   = get_d1_client()
        slug = generate_slug(novel.title)

        # ── Check if novel already exists in D1 ───────────────────────────────
        existing = await d1.query(
            "SELECT id, total_chapters FROM novels WHERE id = ?", [slug]
        )

        if existing and existing[0].get("total_chapters", 0) > 0:
            return NovelUploadResponse(
                title=novel.title,
                author=novel.author,
                chapterCount=len(chapters),
                message="Novel already exists in the database"
            )

        # Make slug unique if collision
        base_slug = slug
        counter = 1
        while True:
            check = await d1.query("SELECT id FROM novels WHERE id = ?", [slug])
            if not check:
                break
            slug = f"{base_slug}-{counter}"
            counter += 1

        # ── Insert novel into D1 ──────────────────────────────────────────────
        await d1.execute(
            """
            INSERT OR REPLACE INTO novels
                (id, title, author, language, total_chapters, created_at, updated_at)
            VALUES (?, ?, ?, 'en', ?, datetime('now'), datetime('now'))
            """,
            [slug, novel.title, novel.author or "Unknown", len(chapters)],
        )
        logger.info("Inserted novel into D1", slug=slug, chapters=len(chapters))

        # ── Upload each chapter: text → R2, metadata → D1 ────────────────────
        for chapter in chapters:
            chap_num      = chapter["chapterNumber"]
            chap_title    = chapter.get("chapterTitle", f"Chapter {chap_num}")
            chap_content  = chapter.get("content", [])   # list of paragraph strings
            word_count    = calculate_word_count(chap_content)

            # Join paragraphs with double newline and gzip-compress
            text      = "\n\n".join(p.strip() for p in chap_content if p.strip())
            r2_key    = upload_chapter_text_to_r2(slug, chap_num, text)

            chapter_id = f"{slug}_ch_{chap_num}"
            await d1.execute(
                """
                INSERT OR REPLACE INTO chapters
                    (id, novel_id, chapter_number, title,
                     r2_content_path, word_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                [chapter_id, slug, chap_num, chap_title, r2_key, word_count],
            )

        logger.info("Uploaded chapters to R2 + D1", slug=slug, count=len(chapters))

        # ── Images still go to Supabase (binary blobs) ───────────────────────
        if images:
            try:
                supabase = get_supabase_client()
                # Resolve int novel ID from Supabase (needed for FK)
                novel_sb = supabase.table('novels').select('id').eq('slug', slug).execute()
                supabase_novel_id = novel_sb.data[0]['id'] if novel_sb.data else None

                if supabase_novel_id:
                    img_batch_size = 10
                    images_data = [
                        {
                            "novel_id":      supabase_novel_id,
                            "image_id":      img["id"],
                            "original_path": img.get("originalPath"),
                            "content_type":  img.get("contentType"),
                            "size":          img.get("size"),
                            "data":          img.get("data"),
                        }
                        for img in images
                    ]
                    for i in range(0, len(images_data), img_batch_size):
                        supabase.table('epub_images').insert(images_data[i:i+img_batch_size]).execute()
                    logger.info("Stored images in Supabase", count=len(images))
            except Exception as img_err:
                logger.error("Error storing images", error=str(img_err))
                # Don't fail the whole upload for image errors

        return NovelUploadResponse(
            title=novel.title,
            author=novel.author,
            chapterCount=len(chapters),
            message=f"Novel uploaded to D1 + R2 with {len(images)} images"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing EPUB file", error=str(e))
        raise HTTPException(status_code=500, detail=f"Error processing EPUB file: {str(e)}")
    finally:
        await file.close()


# ── Image routes (still Supabase — images are binary blobs) ──────────────────

@router.get("/novel/{novel_id}/image/{image_id}")
async def get_novel_image(novel_id: str, image_id: str):
    """Retrieve a single image from Supabase."""
    try:
        supabase = get_supabase_client()

        # Resolve slug → UUID if needed
        actual_id = novel_id
        if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}', novel_id, re.I):
            res = supabase.table('novels').select('id').eq('slug', novel_id).execute()
            if res.data:
                actual_id = res.data[0]['id']
            else:
                raise HTTPException(status_code=404, detail="Novel not found")

        result = supabase.table('epub_images').select('*').eq('novel_id', actual_id).eq('image_id', image_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Image not found")

        image_data = result.data[0]
        try:
            image_bytes = base64.b64decode(image_data["data"])
        except Exception:
            raise HTTPException(status_code=500, detail="Error decoding image data")

        return Response(
            content=image_bytes,
            media_type=image_data.get("content_type", "image/jpeg"),
            headers={
                "Cache-Control": "public, max-age=3600",
                "Content-Disposition": f"inline; filename=\"{image_data.get('original_path', 'image')}\"",
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving image: {str(e)}")


@router.get("/novel/{novel_id}/images", response_model=NovelImagesResponse)
async def get_novel_images_list(novel_id: str):
    """List all images for a novel."""
    try:
        supabase = get_supabase_client()

        actual_id = novel_id
        if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}', novel_id, re.I):
            res = supabase.table('novels').select('id').eq('slug', novel_id).execute()
            if res.data:
                actual_id = res.data[0]['id']
            else:
                raise HTTPException(status_code=404, detail="Novel not found")

        result = supabase.table('epub_images').select(
            'image_id, original_path, content_type, size'
        ).eq('novel_id', actual_id).execute()

        images = [
            ImageInfo(
                id=img["image_id"],
                originalPath=img.get("original_path"),
                contentType=img.get("content_type"),
                size=img.get("size"),
                url=f"/novel/{novel_id}/image/{img['image_id']}",
            )
            for img in result.data
        ]

        return NovelImagesResponse(novelId=novel_id, images=images, count=len(images))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving images list: {str(e)}")
