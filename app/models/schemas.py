from pydantic import BaseModel
from typing import List, Optional

class NovelInfo(BaseModel):
    id: Optional[str] = None
    slug: Optional[str] = None
    title: str
    author: Optional[str] = None
    chapterCount: Optional[int] = None
    source: str
    status: Optional[str] = None
    genres: Optional[List[str]] = None
    description: Optional[str] = None
    hasImages: Optional[bool] = False
    imageCount: Optional[int] = 0

class TTSRequest(BaseModel):
    text: str
    voice: str = "en-US-ChristopherNeural"
    
class TTSDualVoiceRequest(BaseModel):
    text: str
    paragraphVoice: str = "en-US-ChristopherNeural"
    dialogueVoice: str = "en-US-AriaNeural"

class UserLoginRequest(BaseModel):
    username: str
    password: str

class UserRegisterRequest(BaseModel):
    username: str
    password: str

class NovelProgress(BaseModel):
    novelName: str
    lastChapterRead: int

class UserProgressRequest(BaseModel):
    username: str
    novelName: str
    lastChapterRead: int

class UserProgressFetchRequest(BaseModel):
    username: str

class Chapter(BaseModel):
    number: int
    title: str
    content: List[str]

class Novel(BaseModel):
    title: str
    author: str
    chapters: List[Chapter]

class NovelUploadResponse(BaseModel):
    title: str
    author: str
    chapterCount: int
    message: str = "Novel uploaded and parsed successfully"

class ImageInfo(BaseModel):
    id: str
    originalPath: str
    contentType: str
    size: int
    url: str

class NovelImagesResponse(BaseModel):
    novelId: str
    images: List[ImageInfo]
    count: int