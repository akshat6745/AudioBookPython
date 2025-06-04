from pydantic import BaseModel

class TTSRequest(BaseModel):
    text: str
    voice: str = "en-US-ChristopherNeural"

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