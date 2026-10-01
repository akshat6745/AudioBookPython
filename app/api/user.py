from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from app.api.deps import AuthenticatedUser, get_current_user
from app.core.tokens import TokenError, create_access_token
from app.models.schemas import (
    GoogleSignInRequest,
    UserLoginRequest,
    UserRegisterRequest,
    UserProgressRequest,
)
from app.core.d1_client import get_d1_client
from app.core.google_identity import (
    GoogleIdentity,
    GoogleIdentityError,
    google_username,
    looks_like_email,
    verify_google_id_token,
)
from app.core.passwords import (
    NO_PASSWORD_SENTINEL,
    PasswordPolicyError,
    hash_password,
    verify_legacy_plaintext,
    verify_password,
)
from app.core.settings import settings
from app.api.novels import resolve_novel_id
from typing import Optional
import structlog

logger = structlog.get_logger()
router = APIRouter()

# Generic, non-enumerating message for every failed credential check.
_INVALID_CREDENTIALS = "Invalid username or password"


def _session_response(user: dict) -> dict:
    """The payload returned by a successful sign-in.

    Explicitly built field by field rather than spreading the user row, so a
    password hash or any column added later can't leak into a response. The
    token is the one credential here, and it belongs to the requester.
    """
    try:
        token = create_access_token(user_id=user["id"], username=user["username"])
    except TokenError as exc:
        logger.error("Cannot issue access token", reason=str(exc))
        raise HTTPException(status_code=503, detail="Sign-in is unavailable")

    return {
        "status": "success",
        "message": "Login successful",
        "username": user["username"],
        "access_token": token,
        "token_type": "bearer",
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_user(username: str) -> Optional[dict]:
    """Fetch a user row from D1 by username."""
    d1 = get_d1_client()
    rows = await d1.query(
        "SELECT id, username, password, google_sub, auth_provider "
        "FROM users WHERE username = ?",
        [username],
    )
    return rows[0] if rows else None


async def _get_user_by_google_sub(google_sub: str) -> Optional[dict]:
    d1 = get_d1_client()
    rows = await d1.query(
        "SELECT id, username, password, google_sub, auth_provider "
        "FROM users WHERE google_sub = ?",
        [google_sub],
    )
    return rows[0] if rows else None


def _changed_rows(meta: dict) -> int:
    """Rows affected by the last write.

    D1 has reported this under different meta keys across API versions, so
    accept either rather than silently reading 0 and treating a successful
    write as a conflict.
    """
    for key in ("changes", "rows_written", "changed_db"):
        value = meta.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


async def _link_google_to_existing_user(user: dict, identity: GoogleIdentity) -> None:
    """Attach a Google identity to a pre-existing username row.

    Conditional on the row still being unlinked, because D1 gives us no
    transaction — if someone linked first we must not overwrite them.

    The password is replaced with the sentinel in the same statement: the
    account's username is an email address that Google now vouches for, so a
    legacy password on that row is a credential nobody has proven they own.
    """
    d1 = get_d1_client()
    meta = await d1.execute(
        "UPDATE users SET google_sub = ?, email = ?, auth_provider = 'google', "
        "password = ?, updated_at = datetime('now') "
        "WHERE id = ? AND google_sub IS NULL",
        [identity.subject, identity.email, NO_PASSWORD_SENTINEL, user["id"]],
    )
    if _changed_rows(meta) == 0:
        # Someone linked this row between our read and our write.
        current = await _get_user(user["username"])
        if not current or current.get("google_sub") != identity.subject:
            raise HTTPException(status_code=409, detail="Account is already linked")


async def _create_google_user(identity: GoogleIdentity) -> None:
    """Insert a new Google-backed account.

    D1 has no RETURNING, so this is INSERT-then-read by the caller. A
    double-tapped sign-in can race here; the username UNIQUE constraint is
    what makes that safe, so a duplicate insert is treated as "already there".
    """
    d1 = get_d1_client()
    username = google_username(identity)
    try:
        await d1.execute(
            "INSERT INTO users (username, password, google_sub, email, auth_provider) "
            "VALUES (?, ?, ?, ?, 'google')",
            [username, NO_PASSWORD_SENTINEL, identity.subject, identity.email],
        )
    except RuntimeError as exc:
        # UNIQUE violation on a concurrent sign-in — fine, the row now exists.
        if "UNIQUE" not in str(exc).upper():
            raise
        logger.info("Concurrent Google account creation", username=username)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/userLogin")
async def user_login(request: UserLoginRequest):
    user = await _get_user(request.username)
    if not user:
        raise HTTPException(status_code=401, detail=_INVALID_CREDENTIALS)

    stored = user.get("password")
    # bcrypt is CPU-bound; keep it off the event loop.
    if await run_in_threadpool(verify_password, request.password, stored):
        return _session_response(user)

    # TEMPORARY migration-window path — see app/core/passwords.py and the
    # rollout order in scripts/hash_existing_passwords.py. Remove once the
    # migration has run and ALLOW_PLAINTEXT_LOGIN is off for good.
    if settings.ALLOW_PLAINTEXT_LOGIN and verify_legacy_plaintext(
        request.password, stored
    ):
        logger.warning(
            "Login accepted a pre-migration plaintext password",
            username=request.username,
        )
        return _session_response(user)

    raise HTTPException(status_code=401, detail=_INVALID_CREDENTIALS)


@router.post("/register")
async def register_user(request: UserRegisterRequest):
    username = request.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    # Email-shaped usernames belong to Google sign-in. Allowing them here
    # would let anyone pre-register someone else's address and be auto-linked
    # into when that person later signs in with Google.
    if looks_like_email(username):
        raise HTTPException(
            status_code=400,
            detail="Email addresses can't be used as usernames — use Sign in with Google instead",
        )

    if await _get_user(username):
        raise HTTPException(status_code=400, detail="Username already exists")

    # Hash after the cheap existence check — bcrypt costs real CPU.
    try:
        password_hash = await run_in_threadpool(hash_password, request.password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        d1 = get_d1_client()
        await d1.execute(
            "INSERT INTO users (username, password, auth_provider) "
            "VALUES (?, ?, 'password')",
            [username, password_hash],
        )
        return {"status": "success", "message": "User registered successfully"}
    except Exception as e:
        logger.error("Error registering user in D1", error=str(e))
        raise HTTPException(status_code=500, detail="Registration failed")


@router.post("/auth/google")
async def google_sign_in(request: GoogleSignInRequest):
    """Sign in (or register) with a Google ID token.

    The token is verified server-side; nothing the client claims about its own
    identity is trusted. Returns the resolved `username` so the client can
    store it exactly as it stores a password login's username.
    """
    if not settings.GOOGLE_CLIENT_ID:
        logger.error("Google sign-in attempted but GOOGLE_CLIENT_ID is unset")
        raise HTTPException(
            status_code=503, detail="Google sign-in is not configured"
        )

    try:
        identity = await run_in_threadpool(
            verify_google_id_token, request.idToken, settings.GOOGLE_CLIENT_ID
        )
    except GoogleIdentityError as exc:
        # Log the reason, return a generic message — and never the token.
        logger.warning("Rejected Google ID token", reason=str(exc))
        raise HTTPException(status_code=401, detail="Could not verify Google sign-in")

    try:
        user = await _get_user_by_google_sub(identity.subject)
        if user:
            return _session_response(user)

        username = google_username(identity)
        existing = await _get_user(username)
        if existing:
            await _link_google_to_existing_user(existing, identity)
            logger.info(
                "Linked Google identity to existing account",
                username=username,
                google_sub=identity.subject,
            )
            return _session_response(existing)

        await _create_google_user(identity)
        # D1 has no RETURNING, so re-read the row to get its generated id —
        # the token's `sub` has to be the real primary key.
        created = await _get_user(username)
        if not created:
            logger.error("Google account vanished after creation", username=username)
            raise HTTPException(status_code=500, detail="Google sign-in failed")
        logger.info(
            "Created account from Google sign-in",
            username=username,
            google_sub=identity.subject,
        )
        return _session_response(created)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Google sign-in failed", error=str(e))
        raise HTTPException(status_code=500, detail="Google sign-in failed")


@router.post("/auth/google/link")
async def link_google_account(
    request: GoogleSignInRequest,
    caller: AuthenticatedUser = Depends(get_current_user),
):
    """Attach a Google identity to the signed-in account.

    Needs two proofs: the access token shows the caller owns this account,
    and the verified Google ID token shows they own the Google identity.
    Unlike the email auto-link in /auth/google, the password is kept — the
    owner has just proven themselves, so both sign-in methods stay usable.
    Afterwards /auth/google finds this row by `google_sub`.
    """
    if not settings.GOOGLE_CLIENT_ID:
        logger.error("Google link attempted but GOOGLE_CLIENT_ID is unset")
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")

    try:
        identity = await run_in_threadpool(
            verify_google_id_token, request.idToken, settings.GOOGLE_CLIENT_ID
        )
    except GoogleIdentityError as exc:
        logger.warning("Rejected Google ID token for linking", reason=str(exc))
        raise HTTPException(status_code=401, detail="Could not verify Google sign-in")

    user = await _get_user(caller.username)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Re-linking the same Google account is a no-op, not an error.
    if user.get("google_sub") == identity.subject:
        return {"status": "success", "linked": True, "email": identity.email}

    if user.get("google_sub"):
        raise HTTPException(
            status_code=409,
            detail="This account is already linked to a different Google account",
        )

    # One account per Google identity. Refuse rather than move it, so a
    # link can never silently take sign-in away from another account.
    if await _get_user_by_google_sub(identity.subject):
        raise HTTPException(
            status_code=409,
            detail="That Google account is already linked to another account",
        )

    try:
        d1 = get_d1_client()
        meta = await d1.execute(
            "UPDATE users SET google_sub = ?, email = ?, updated_at = datetime('now') "
            "WHERE id = ? AND google_sub IS NULL",
            [identity.subject, identity.email, user["id"]],
        )
    except RuntimeError as exc:
        # The unique index on google_sub: another account linked it between
        # our check and this write.
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(
                status_code=409,
                detail="That Google account is already linked to another account",
            )
        logger.error("Google link failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Linking failed")

    if _changed_rows(meta) == 0:
        # This row was linked concurrently; don't report success.
        raise HTTPException(
            status_code=409,
            detail="This account is already linked to a different Google account",
        )

    logger.info(
        "Linked Google identity to signed-in account",
        username=caller.username,
        google_sub=identity.subject,
    )
    return {"status": "success", "linked": True, "email": identity.email}


@router.post("/user/progress")
async def save_user_progress(
    request: UserProgressRequest,
    caller: AuthenticatedUser = Depends(get_current_user),
):
    # Identity comes from the verified token, never from the request body.
    # This write sets a user_id foreign key, so the row is resolved from the
    # database rather than trusting the token's `sub`.
    user = await _get_user(caller.username)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

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

        rows = await d1.query(
            "SELECT updated_at FROM user_progress WHERE user_id = ? AND novel_id = ?",
            [user_id, novel_id],
        )
        last_read_date = rows[0]["updated_at"] if rows else None

        logger.info("Saved progress to D1", username=caller.username, novel=request.novelName)
        return {"status": "success", "message": "Progress saved", "lastReadDate": last_read_date}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error saving progress to D1", error=str(e))
        raise HTTPException(status_code=500, detail="Error saving progress")


@router.get("/user/progress")
async def get_all_user_progress(
    caller: AuthenticatedUser = Depends(get_current_user),
):
    user = await _get_user(caller.username)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    d1 = get_d1_client()
    rows = await d1.query(
        "SELECT novel_id, chapter_number, updated_at FROM user_progress WHERE user_id = ?",
        [user["id"]],
    )
    progress = [
        {"novelName": r["novel_id"], "lastChapterRead": r["chapter_number"], "lastReadDate": r["updated_at"]}
        for r in rows
    ]
    return {"progress": progress}


@router.get("/user/progress/{novelName}")
async def get_user_progress_for_novel(
    novelName: str,
    caller: AuthenticatedUser = Depends(get_current_user),
):
    user = await _get_user(caller.username)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    d1 = get_d1_client()
    novel_id = await resolve_novel_id(d1, novelName)
    
    rows = await d1.query(
        "SELECT chapter_number, updated_at FROM user_progress WHERE user_id = ? AND novel_id = ?",
        [user["id"], novel_id],
    )
    last = rows[0]["chapter_number"] if rows else 1
    last_read_date = rows[0]["updated_at"] if rows else None
    return {"novelName": novelName, "lastChapterRead": last, "lastReadDate": last_read_date}