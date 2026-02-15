from fastapi import APIRouter, UploadFile, HTTPException
from fastapi.responses import Response
from app.models.schemas import NovelUploadResponse, ImageInfo, NovelImagesResponse
from app.services.epub_parser import parse_epub_content
from app.core.supabase_client import get_supabase_client
import re
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
    """Calculate total word count from a list of paragraphs."""
    return sum(len(p.split()) for p in content)

def verify_upload(epub_content: bytes, novel_id: str, supabase, logger) -> bool:
    """
    Verify that uploaded content matches database content.
    Returns True if verification passed, False otherwise.
    """
    try:
        # Re-parse the EPUB
        novel, parsed_chapters, _ = parse_epub_content(epub_content)
        
        # Get chapters from database
        db_chapters = supabase.table('chapters').select(
            'chapter_number, chapter_title, content'
        ).eq('novel_id', novel_id).order('chapter_number').execute()
        
        # Quick verification
        if len(parsed_chapters) != len(db_chapters.data):
            logger.error(
                "Verification failed: chapter count mismatch",
                parsed=len(parsed_chapters),
                db=len(db_chapters.data),
                novel_id=novel_id
            )
            return False
        
        # Verify first, middle, and last chapter content
        check_indices = [0, len(parsed_chapters) // 2, len(parsed_chapters) - 1]
        
        for idx in check_indices:
            if idx < len(parsed_chapters):
                parsed_ch = parsed_chapters[idx]
                db_ch = db_chapters.data[idx]
                
                if parsed_ch['chapterTitle'] != db_ch['chapter_title']:
                    logger.error(
                        "Verification failed: title mismatch",
                        chapter=idx+1,
                        parsed=parsed_ch['chapterTitle'],
                        db=db_ch['chapter_title'],
                        novel_id=novel_id
                    )
                    return False
                
                parsed_content = ' '.join(parsed_ch['content'])
                db_content = ' '.join(db_ch['content'])
                
                if parsed_content != db_content:
                    logger.error(
                        "Verification failed: content mismatch",
                        chapter=idx+1,
                        parsed_len=len(parsed_content),
                        db_len=len(db_content),
                        novel_id=novel_id
                    )
                    return False
        
        logger.info("Content verification passed", novel_id=novel_id, chapters=len(parsed_chapters))
        return True
        
    except Exception as e:
        logger.error("Verification error", error=str(e), novel_id=novel_id)
        return False


@router.post("/upload-epub", response_model=NovelUploadResponse)
async def upload_epub(file: UploadFile):
    """
    Upload and parse an EPUB file.
    Novel, chapters, and images are stored in Supabase.
    """
    if not file.filename.endswith('.epub'):
        raise HTTPException(status_code=400, detail="File must be an EPUB file")
    
    try:
        content = await file.read()
        novel, chapters, images = parse_epub_content(content)
        
        supabase = get_supabase_client()
        slug = generate_slug(novel.title)
        
        # Search for existing novel by slug or exact title
        # Using separate queries to avoid PostgREST syntax errors with special characters in titles
        existing = supabase.table('novels').select('id, slug').eq('slug', slug).execute()
        if not existing.data:
            existing = supabase.table('novels').select('id, slug').eq('title', novel.title).execute()
        
        if existing.data:
            existing_novel_id = existing.data[0]['id']
            existing_chapters = supabase.table('chapters').select('id').eq('novel_id', existing_novel_id).limit(1).execute()
            
            if existing_chapters.data:
                return NovelUploadResponse(
                    title=novel.title,
                    author=novel.author,
                    chapterCount=len(chapters),
                    message="Novel already exists in the database"
                )
            else:
                logger.info("Found orphaned novel with 0 chapters, deleting", novel_id=existing_novel_id)
                supabase.table('novels').delete().eq('id', existing_novel_id).execute()
        
        base_slug = slug
        counter = 1
        while True:
            slug_check = supabase.table('novels').select('id').eq('slug', slug).execute()
            if not slug_check.data:
                break
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        novel_data = {
            "slug": slug,
            "title": novel.title,
            "author": novel.author or "Unknown",
            "status": "uploaded",
            "description": None,
            "genres": None,
        }
        
        novel_result = supabase.table('novels').insert(novel_data).execute()
        
        if not novel_result.data:
            raise HTTPException(status_code=500, detail="Failed to insert novel into Supabase")
        
        novel_id = novel_result.data[0]['id']
        logger.info("Inserted novel into Supabase", novel_id=novel_id, slug=slug)
        
        chapters_data = []
        for chapter in chapters:
            chapter_content = chapter.get("content", [])
            chapters_data.append({
                "novel_id": novel_id,
                "chapter_number": chapter["chapterNumber"],
                "chapter_title": chapter.get("chapterTitle", f"Chapter {chapter['chapterNumber']}"),
                "content": chapter_content,
                "word_count": calculate_word_count(chapter_content),
            })
        
        if chapters_data:
            try:
                batch_size = 50
                for i in range(0, len(chapters_data), batch_size):
                    batch = chapters_data[i:i+batch_size]
                    supabase.table('chapters').insert(batch).execute()
                logger.info("Inserted chapters into Supabase", count=len(chapters_data))
            except Exception as chapter_error:
                logger.error("Failed to insert chapters, rolling back novel", error=str(chapter_error))
                supabase.table('novels').delete().eq('id', novel_id).execute()
                raise HTTPException(status_code=500, detail=f"Failed to insert chapters: {str(chapter_error)}")
        
        # Verify uploaded content if enabled
        from app.core.settings import get_settings
        settings = get_settings()
        
        if settings.VERIFY_UPLOADS:
            logger.info("Verifying uploaded content", novel_id=novel_id)
            verification_passed = verify_upload(content, novel_id, supabase, logger)
            
            if not verification_passed:
                logger.warning("Content verification failed - data may be incomplete", novel_id=novel_id)
                # Don't fail the upload, just log the warning
        
        if images:
            images_data = []
            for image in images:
                images_data.append({
                    "novel_id": novel_id,
                    "image_id": image["id"],
                    "original_path": image.get("originalPath"),
                    "content_type": image.get("contentType"),
                    "size": image.get("size"),
                    "data": image.get("data")
                })
            
            if images_data:
                try:
                    # Insert images in batches
                    img_batch_size = 10
                    for i in range(0, len(images_data), img_batch_size):
                        supabase.table('epub_images').insert(images_data[i:i+img_batch_size]).execute()
                    logger.info("Stored images in Supabase", count=len(images))
                except Exception as img_error:
                    logger.error("Error storing images in Supabase", error=str(img_error))
                    # We don't roll back the whole novel if images fail, just log it
            
        return NovelUploadResponse(
            title=novel.title,
            author=novel.author,
            chapterCount=len(chapters),
            message=f"Novel uploaded to Supabase with {len(images)} images"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing EPUB file", error=str(e))
        raise HTTPException(status_code=500, detail=f"Error processing EPUB file: {str(e)}")
    finally:
        await file.close()

@router.get("/novel/{novel_id}/image/{image_id}")
async def get_novel_image(novel_id: str, image_id: str):
    """
    Retrieve an image from a specific novel by image ID.
    novel_id can be either the UUID or the novel slug.
    """
    try:
        supabase = get_supabase_client()
        
        # Resolve novel_id to UUID if it's a slug
        actual_novel_id = novel_id
        if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', novel_id, re.I):
            res = supabase.table('novels').select('id').eq('slug', novel_id).execute()
            if res.data:
                actual_novel_id = res.data[0]['id']
            else:
                raise HTTPException(status_code=404, detail="Novel not found")
        
        # Get the image from Supabase
        result = supabase.table('epub_images').select('*').eq('novel_id', actual_novel_id).eq('image_id', image_id).execute()
        
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
    """
    Get a list of all images in a novel with their metadata.
    """
    try:
        supabase = get_supabase_client()
        
        # Resolve novel_id to UUID if it's a slug
        actual_novel_id = novel_id
        if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', novel_id, re.I):
            res = supabase.table('novels').select('id').eq('slug', novel_id).execute()
            if res.data:
                actual_novel_id = res.data[0]['id']
            else:
                raise HTTPException(status_code=404, detail="Novel not found")
        
        # Get all images from Supabase
        result = supabase.table('epub_images').select('image_id, original_path, content_type, size').eq('novel_id', actual_novel_id).execute()
        
        images = []
        for img in result.data:
            images.append(ImageInfo(
                id=img["image_id"],
                originalPath=img.get("original_path"),
                contentType=img.get("content_type"),
                size=img.get("size"),
                url=f"/novel/{novel_id}/image/{img['image_id']}"
            ))
        
        return NovelImagesResponse(
            novelId=novel_id,
            images=images,
            count=len(images)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving images list: {str(e)}")
