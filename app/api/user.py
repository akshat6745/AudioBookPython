from fastapi import APIRouter, HTTPException
from app.models.schemas import UserLoginRequest, UserRegisterRequest, UserProgressRequest
from app.core.db import db

router = APIRouter()
users_collection = db.collection("users")

@router.post("/userLogin")
def user_login(request: UserLoginRequest):
    user_docs = users_collection.where("username", "==", request.username).stream()
    user = next(user_docs, None)
    if user and user.to_dict().get("password") == request.password:
        return {"status": "success", "message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid username or password")

@router.post("/register")
def register_user(request: UserRegisterRequest):
    user_docs = users_collection.where("username", "==", request.username).stream()
    if next(user_docs, None):
        raise HTTPException(status_code=400, detail="Username already exists")
    users_collection.add({"username": request.username, "password": request.password, "progress": []})
    return {"status": "success", "message": "User registered successfully"}

@router.post("/user/progress")
def save_user_progress(request: UserProgressRequest):
    user_docs = users_collection.where("username", "==", request.username).stream()
    user_doc = next(user_docs, None)
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")
    user_data = user_doc.to_dict()
    progress = user_data.get("progress", [])
    updated = False
    for entry in progress:
        if entry["novelName"] == request.novelName:
            entry["lastChapterRead"] = request.lastChapterRead
            updated = True
            break
    if not updated:
        progress.append({"novelName": request.novelName, "lastChapterRead": request.lastChapterRead})
    users_collection.document(user_doc.id).update({"progress": progress})
    return {"status": "success", "message": "Progress saved"}

@router.get("/user/progress")
def get_all_user_progress(username: str):
    user_docs = users_collection.where("username", "==", username).stream()
    user_doc = next(user_docs, None)
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")
    return {"progress": user_doc.to_dict().get("progress", [])}

@router.get("/user/progress/{novelName}")
def get_user_progress_for_novel(novelName: str, username: str):
    user_docs = users_collection.where("username", "==", username).stream()
    user_doc = next(user_docs, None)
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")
    progress = user_doc.to_dict().get("progress", [])
    for entry in progress:
        if entry["novelName"] == novelName:
            return {"novelName": novelName, "lastChapterRead": entry["lastChapterRead"]}
    return {"novelName": novelName, "lastChapterRead": 1} 