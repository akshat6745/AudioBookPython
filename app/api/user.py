from fastapi import APIRouter, HTTPException
from app.models.schemas import UserLoginRequest, UserRegisterRequest, UserProgressRequest
from app.core.d1_client import get_d1_client
from app.api.novels import resolve_novel_id
from typing import Optional
import structlog

logger = structlog.get_logger()
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_user(username: str) -> Optional[dict]:
    """Fetch a user row from D1 by username."""
    d1 = get_d1_client()
    rows = await d1.query(
        "SELECT id, username, password FROM users WHERE username = ?",
        [username],
    )
    return rows[0] if rows else None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/userLogin")
async def user_login(request: UserLoginRequest):
    user = await _get_user(request.username)
    if user and user.get("password") == request.password:
        return {"status": "success", "message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid username or password")


@router.post("/register")
async def register_user(request: UserRegisterRequest):
    if await _get_user(request.username):
        raise HTTPException(status_code=400, detail="Username already exists")

    try:
        d1 = get_d1_client()
        await d1.execute(
            "INSERT INTO users (username, password) VALUES (?, ?)",
            [request.username, request.password],
        )
        return {"status": "success", "message": "User registered successfully"}
    except Exception as e:
        logger.error("Error registering user in D1", error=str(e))
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")


@router.post("/user/progress")
async def save_user_progress(request: UserProgressRequest):
    user = await _get_user(request.username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        d1 = get_d1_client()
        user_id = user["id"]
        novel_id = await resolve_novel_id(d1, request.novelName)

        # Upsert into user_progress (INSERT OR REPLACE updates if UNIQUE constraint fires)
        await d1.execute(
            """
            INSERT INTO user_progress (user_id, novel_id, chapter_number, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, novel_id)
            DO UPDATE SET chapter_number = excluded.chapter_number,
                          updated_at     = excluded.updated_at
            """,
            [user_id, novel_id, request.lastChapterRead],
        )
        logger.info("Saved progress to D1", username=request.username, novel=request.novelName)
        return {"status": "success", "message": "Progress saved"}
    except Exception as e:
        logger.error("Error saving progress to D1", error=str(e))
        raise HTTPException(status_code=500, detail=f"Error saving progress: {str(e)}")


@router.get("/user/progress")
async def get_all_user_progress(username: str):
    user = await _get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    d1 = get_d1_client()
    rows = await d1.query(
        "SELECT novel_id, chapter_number FROM user_progress WHERE user_id = ?",
        [user["id"]],
    )
    progress = [
        {"novelName": r["novel_id"], "lastChapterRead": r["chapter_number"]}
        for r in rows
    ]
    return {"progress": progress}


@router.get("/user/progress/{novelName}")
async def get_user_progress_for_novel(novelName: str, username: str):
    user = await _get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    d1 = get_d1_client()
    novel_id = await resolve_novel_id(d1, novelName)
    
    rows = await d1.query(
        "SELECT chapter_number FROM user_progress WHERE user_id = ? AND novel_id = ?",
        [user["id"], novel_id],
    )
    last = rows[0]["chapter_number"] if rows else 1
    return {"novelName": novelName, "lastChapterRead": last}