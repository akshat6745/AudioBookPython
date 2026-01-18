from fastapi import APIRouter, UploadFile, HTTPException
from fastapi.responses import Response
from app.models.schemas import NovelUploadResponse, ImageInfo, NovelImagesResponse
from app.services.epub_parser import parse_epub_content
from app.core.db import db
from app.core.supabase_client import get_supabase_client
import re
import base64
import structlog

router = APIRouter()
logger = structlog.get_logger()

# Firebase collection for images only (hybrid approach)
firebase_novels_collection = db.collection("epub_images")


def generate_slug(title: str) -> str:
    """Generate a URL-friendly slug from a title."""
    # Convert to lowercase
    slug = title.lower()
    # Replace spaces and special chars with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    # Limit length
    return slug[:200] if len(slug) > 200 else slug


def calculate_word_count(content: list) -> int:
    """Calculate total word count from a list of paragraphs."""
    return sum(len(p.split()) for p in content)


@router.post("/upload-epub", response_model=NovelUploadResponse)
async def upload_epub(file: UploadFile):
    """
    Upload and parse an EPUB file.
    Novel and chapters are stored in Supabase; images remain in Firebase.
    """
    if not file.filename.endswith('.epub'):
        raise HTTPException(status_code=400, detail="File must be an EPUB file")
    
    try:
        # Read the EPUB file content
        content = await file.read()
        
        # Parse the EPUB content
        novel, chapters, images = parse_epub_content(content)
        
        supabase = get_supabase_client()
        slug = generate_slug(novel.title)
        
        # Check if novel already exists in Supabase by slug or title
        existing = supabase.table('novels').select('id, slug').or_(
            f"slug.eq.{slug},title.eq.{novel.title}"
        ).execute()
        
        if existing.data:
            existing_novel_id = existing.data[0]['id']
            # Check if chapters exist - if not, this is an orphaned novel from failed upload
            existing_chapters = supabase.table('chapters').select('id').eq('novel_id', existing_novel_id).limit(1).execute()
            
            if existing_chapters.data:
                # Novel exists with chapters - truly already uploaded
                return NovelUploadResponse(
                    title=novel.title,
                    author=novel.author,
                    chapterCount=len(chapters),
                    message="Novel already exists in the database"
                )
            else:
                # Orphaned novel (0 chapters) - delete and re-insert
                logger.info("Found orphaned novel with 0 chapters, deleting", novel_id=existing_novel_id)
                supabase.table('novels').delete().eq('id', existing_novel_id).execute()
        
        # Ensure unique slug by appending number if needed
        base_slug = slug
        counter = 1
        while True:
            slug_check = supabase.table('novels').select('id').eq('slug', slug).execute()
            if not slug_check.data:
                break
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        # Insert novel into Supabase
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
        
        # Insert chapters into Supabase in batches
        chapters_data = []
        for chapter in chapters:
            chapter_content = chapter.get("content", [])
            chapters_data.append({
                "novel_id": novel_id,
                "chapter_number": chapter["chapterNumber"],
                "chapter_title": chapter.get("chapterTitle", f"Chapter {chapter['chapterNumber']}"),
                "content": chapter_content,  # TEXT[] in Supabase
                "word_count": calculate_word_count(chapter_content),
            })
        
        # Batch insert chapters in chunks to avoid timeout/size limits
        if chapters_data:
            try:
                batch_size = 50
                for i in range(0, len(chapters_data), batch_size):
                    batch = chapters_data[i:i+batch_size]
                    supabase.table('chapters').insert(batch).execute()
                logger.info("Inserted chapters into Supabase", count=len(chapters_data))
            except Exception as chapter_error:
                # If chapter insert fails, delete the novel to avoid orphaned entry
                logger.error("Failed to insert chapters, rolling back novel", error=str(chapter_error))
                supabase.table('novels').delete().eq('id', novel_id).execute()
                raise HTTPException(status_code=500, detail=f"Failed to insert chapters: {str(chapter_error)}")
        
        
        # Store images in Firebase (hybrid approach - images stay in Firebase)
        firebase_novel_id = None
        if images:
            novel_ref = firebase_novels_collection.document()
            firebase_novel_id = novel_ref.id
            novel_ref.set({
                "supabase_novel_id": novel_id,
                "slug": slug,
                "title": novel.title,
                "imageCount": len(images)
            })
            
            images_collection = novel_ref.collection("images")
            for image in images:
                image_ref = images_collection.document(image["id"])
                image_ref.set(image)
            
            logger.info("Stored images in Firebase", count=len(images), firebase_id=firebase_novel_id)
            
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
    Images are stored in Firebase under epub_images collection.
    novel_id can be either the Firebase document ID or the Supabase novel slug.
    """
    try:
        novel_ref = None
        
        # First try direct Firebase lookup
        direct_ref = firebase_novels_collection.document(novel_id)
        direct_doc = direct_ref.get()
        
        if direct_doc.exists:
            novel_ref = direct_ref
        else:
            # Try to find by slug (for Supabase novels)
            query = firebase_novels_collection.where("slug", "==", novel_id).limit(1).stream()
            for doc in query:
                novel_ref = doc.reference
                break
        
        if not novel_ref:
            raise HTTPException(status_code=404, detail="Novel not found")
        
        # Get the image from the images subcollection
        image_ref = novel_ref.collection("images").document(image_id)
        image_doc = image_ref.get()
        
        if not image_doc.exists:
            raise HTTPException(status_code=404, detail="Image not found")
        
        image_data = image_doc.to_dict()
        
        # Decode the base64 image data
        try:
            image_bytes = base64.b64decode(image_data["data"])
        except Exception as e:
            raise HTTPException(status_code=500, detail="Error decoding image data")
        
        # Return the image with appropriate content type
        return Response(
            content=image_bytes,
            media_type=image_data.get("contentType", "image/jpeg"),
            headers={
                "Cache-Control": "public, max-age=3600",
                "Content-Disposition": f"inline; filename=\"{image_data.get('originalPath', 'image')}\"",
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
        novel_ref = None
        
        # First try direct Firebase lookup
        direct_ref = firebase_novels_collection.document(novel_id)
        direct_doc = direct_ref.get()
        
        if direct_doc.exists:
            novel_ref = direct_ref
        else:
            # Try to find by slug
            query = firebase_novels_collection.where("slug", "==", novel_id).limit(1).stream()
            for doc in query:
                novel_ref = doc.reference
                break
        
        if not novel_ref:
            raise HTTPException(status_code=404, detail="Novel not found or has no images")
        
        # Get all images from the images subcollection
        images_collection = novel_ref.collection("images")
        images = []
        
        for image_doc in images_collection.stream():
            image_data = image_doc.to_dict()
            image_info = ImageInfo(
                id=image_doc.id,
                originalPath=image_data.get("originalPath"),
                contentType=image_data.get("contentType"),
                size=image_data.get("size"),
                url=f"/novel/{novel_id}/image/{image_doc.id}"
            )
            images.append(image_info)
        
        return NovelImagesResponse(
            novelId=novel_id,
            images=images,
            count=len(images)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving images list: {str(e)}")
