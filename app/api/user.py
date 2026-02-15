from fastapi import APIRouter, HTTPException
from app.models.schemas import UserLoginRequest, UserRegisterRequest, UserProgressRequest
from app.core.supabase_client import get_supabase_client
import structlog

logger = structlog.get_logger()
router = APIRouter()

def get_user_from_supabase(username: str):
    """Fetch user from Supabase."""
    try:
        supabase = get_supabase_client()
        result = supabase.table("users_data").select("*").eq("username", username).execute()
        if result.data:
            return result.data[0]
    except Exception as e:
        logger.error("Error fetching user from Supabase", username=username, error=str(e))
    return None

@router.post("/userLogin")
def user_login(request: UserLoginRequest):
    user = get_user_from_supabase(request.username)
    if user and user.get("password") == request.password:
        return {"status": "success", "message": "Login successful"}
    
    raise HTTPException(status_code=401, detail="Invalid username or password")

@router.post("/register")
def register_user(request: UserRegisterRequest):
    if get_user_from_supabase(request.username):
        raise HTTPException(status_code=400, detail="Username already exists")
    
    try:
        supabase = get_supabase_client()
        supabase.table("users_data").insert({
            "username": request.username,
            "password": request.password,
            "progress": []
        }).execute()
        return {"status": "success", "message": "User registered successfully"}
    except Exception as e:
        logger.error("Error registering user", error=str(e))
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")

@router.post("/user/progress")
def save_user_progress(request: UserProgressRequest):
    try:
        supabase = get_supabase_client()
        user = get_user_from_supabase(request.username)
        if not user:
             raise HTTPException(status_code=404, detail="User not found")
             
        progress = user.get("progress", [])
        updated = False
        for entry in progress:
            if entry["novelName"] == request.novelName:
                entry["lastChapterRead"] = request.lastChapterRead
                updated = True
                break
        if not updated:
            progress.append({"novelName": request.novelName, "lastChapterRead": request.lastChapterRead})
        
        supabase.table("users_data").update({"progress": progress}).eq("username", request.username).execute()
        return {"status": "success", "message": "Progress saved"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error saving progress to Supabase", error=str(e))
        raise HTTPException(status_code=500, detail=f"Error saving progress: {str(e)}")

@router.get("/user/progress")
def get_all_user_progress(username: str):
    user = get_user_from_supabase(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {"progress": user.get("progress", [])}

@router.get("/user/progress/{novelName}")
def get_user_progress_for_novel(novelName: str, username: str):
    user = get_user_from_supabase(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    progress = user.get("progress", [])
    for entry in progress:
        if entry["novelName"] == novelName:
            return {"novelName": novelName, "lastChapterRead": entry["lastChapterRead"]}
    return {"novelName": novelName, "lastChapterRead": 1}