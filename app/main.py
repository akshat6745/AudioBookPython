from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import aiohttp
from app.api import novels, tts, user, epub

session = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global session
    session = aiohttp.ClientSession()
    # Set the session in the novels module
    import app.api.novels as novels_module
    novels_module.session = session
    yield
    if session:
        await session.close()

app = FastAPI(title="Novel Reader API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(novels.router)
app.include_router(tts.router)
app.include_router(user.router)
app.include_router(epub.router)

@app.get("/health", status_code=200)
def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    from config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)