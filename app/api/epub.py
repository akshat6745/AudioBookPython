from fastapi import APIRouter, UploadFile, HTTPException
from app.models.schemas import NovelUploadResponse
from app.services.epub_parser import parse_epub_content
from app.core.db import db
import os

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
        novel, chapters = parse_epub_content(content)
        
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
            "id": novel_ref.id
        }
        
        # Store the novel document first
        novel_ref.set(novel_data)
        
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
            message="Novel successfully uploaded and stored"
        )
        
        # Store chapters in a subcollection
        chapters_collection = novel_ref.collection("chapters")
        for chapter in chapters:
            chapter_ref = chapters_collection.document()
            chapter["id"] = chapter_ref.id
            chapter_ref.set(chapter)
        
        # Store the novel document
        novel_ref.set(novel_data)
        
        return NovelUploadResponse(
            title=novel.title,
            author=novel.author,
            chapterCount=len(chapters)
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing EPUB file: {str(e)}")
    finally:
        await file.close()
