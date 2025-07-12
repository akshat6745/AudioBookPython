from fastapi import APIRouter, UploadFile, HTTPException
from fastapi.responses import Response
from app.models.schemas import NovelUploadResponse, ImageInfo, NovelImagesResponse
from app.services.epub_parser import parse_epub_content
from app.core.db import db
import os
import base64

router = APIRouter()
novels_collection = db.collection("novels")

@router.post("/upload-epub", response_model=NovelUploadResponse)
async def upload_epub(file: UploadFile):
    """
    Upload and parse an EPUB file.
    The file will be parsed and stored in Firestore for efficient retrieval.
    """
    if not file.filename.endswith('.epub'):
        raise HTTPException(status_code=400, detail="File must be an EPUB file")
    
    try:
        # Read the EPUB file content
        content = await file.read()
        
        # Parse the EPUB content
        novel, chapters, images = parse_epub_content(content)
        
        # Check if novel already exists
        existing_novels = novels_collection.where("title", "==", novel.title).where("author", "==", novel.author).stream()
        existing_novel = next(existing_novels, None)
        
        if existing_novel:
            return NovelUploadResponse(
                title=novel.title,
                author=novel.author,
                chapterCount=existing_novel.get("chapterCount"),
                message="Novel already exists in the database"
            )
            
        # Create a new novel document
        novel_ref = novels_collection.document()
        novel_data = {
            "title": novel.title,
            "author": novel.author,
            "chapterCount": len(chapters),
            "source": "epub_upload",
            "id": novel_ref.id,
            "hasImages": len(images) > 0,
            "imageCount": len(images)
        }
        
        # Store the novel document first
        novel_ref.set(novel_data)
        
        # Store images in a subcollection
        if images:
            images_collection = novel_ref.collection("images")
            for image in images:
                image_ref = images_collection.document(image["id"])
                image_ref.set(image)
        
        # Store chapters in a subcollection
        chapters_collection = novel_ref.collection("chapters")
        for chapter in chapters:
            chapter_ref = chapters_collection.document(str(chapter["chapterNumber"]))
            chapter["id"] = chapter_ref.id
            chapter_ref.set(chapter)
            
        return NovelUploadResponse(
            title=novel.title,
            author=novel.author,
            chapterCount=len(chapters),
            message=f"Novel successfully uploaded and stored with {len(images)} images"
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing EPUB file: {str(e)}")
    finally:
        await file.close()

@router.get("/novel/{novel_id}/image/{image_id}")
async def get_novel_image(novel_id: str, image_id: str):
    """
    Retrieve an image from a specific novel by image ID.
    Returns the image data with appropriate content type.
    """
    try:
        # Get the novel document
        novel_ref = novels_collection.document(novel_id)
        novel_doc = novel_ref.get()
        
        if not novel_doc.exists:
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
                "Cache-Control": "public, max-age=3600",  # Cache for 1 hour
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
        # Get the novel document
        novel_ref = novels_collection.document(novel_id)
        novel_doc = novel_ref.get()
        
        if not novel_doc.exists:
            raise HTTPException(status_code=404, detail="Novel not found")
        
        # Get all images from the images subcollection
        images_collection = novel_ref.collection("images")
        images = []
        
        for image_doc in images_collection.stream():
            image_data = image_doc.to_dict()
            # Don't include the actual image data in the list, just metadata
            image_info = ImageInfo(
                id=image_doc.id,
                originalPath=image_data.get("originalPath"),
                contentType=image_data.get("contentType"),
                size=image_data.get("size"),
                url=f"/novel/{novel_id}/image/{image_doc.id}"  # URL to access the image
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
