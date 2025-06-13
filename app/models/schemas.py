from pydantic import BaseModel
from typing import List, Optional

class NovelInfo(BaseModel):
    id: Optional[str] = None
    title: str
    author: Optional[str] = None
    chapterCount: Optional[int] = None
    source: str

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