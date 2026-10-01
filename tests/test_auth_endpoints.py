"""Handler tests for /userLogin, /register and /auth/google.

D1 is replaced with a small in-memory fake (this repo has no D1 mocking, and
standing up real Cloudflare access in tests is not worth it) and Google token
verification is stubbed, so no network call happens. What is under test is our
own logic: hash verification, the plaintext migration gate, the email
namespace rule, and Google account resolution/linking.

The route functions are awaited directly rather than driven through
Starlette's TestClient — the installed starlette/httpx pair has an
incompatible TestClient constructor, and pinning around it isn't worth it when
the handlers are plain coroutines.

Run: python -m pytest tests/test_auth_endpoints.py
"""

import json
import re
import sys
import types
import uuid

import pytest
from fastapi import HTTPException

# app.api.user imports app.api.novels, which pulls in boto3-backed services.
# Stub that module if the storage stack isn't installed locally.
try:  # pragma: no cover
    import app.services.cloudflare_service  # noqa: F401
except Exception:  # pragma: no cover
    stub = types.ModuleType("app.services.cloudflare_service")
    stub.get_chapter_paragraphs = lambda *a, **k: []
    stub.upload_chapter_text_to_r2 = lambda *a, **k: ""
    stub._r2_client = lambda: None
    stub._r2_bucket = lambda: ""
    sys.modules["app.services.cloudflare_service"] = stub

from app.api import user as user_api  # noqa: E402
from app.api.deps import AuthenticatedUser, get_current_user  # noqa: E402
from app.core.google_identity import GoogleIdentity, GoogleIdentityError  # noqa: E402
from app.core.passwords import NO_PASSWORD_SENTINEL, hash_password  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.core.tokens import decode_access_token  # noqa: E402
from app.models.schemas import (  # noqa: E402
    GoogleSignInRequest,
    UserLoginRequest,
    UserProgressRequest,
    UserRegisterRequest,
)

def _run(coro):
    """Await a handler coroutine (no pytest-asyncio in this repo)."""
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


class FakeD1:
    """Enough of the D1 client for the auth handlers: a list of user dicts."""

    def __init__(self, users=None):
        self.users = list(users or [])

    async def query(self, sql, params=None):
        params = params or []
        if "WHERE username = ?" in sql:
            return [dict(u) for u in self.users if u["username"] == params[0]]
        if "WHERE google_sub = ?" in sql:
            return [dict(u) for u in self.users if u.get("google_sub") == params[0]]
        return [dict(u) for u in self.users]

    async def execute(self, sql, params=None):
        params = params or []
        if sql.startswith("INSERT INTO users"):
            username = params[0]
            if any(u["username"] == username for u in self.users):
                raise RuntimeError("D1 error: UNIQUE constraint failed: users.username")
            has_google = len(params) > 2
            self.users.append(
                {
                    "id": uuid.uuid4().hex,
                    "username": username,
                    "password": params[1],
                    "google_sub": params[2] if has_google else None,
                    "email": params[3] if has_google else None,
                    "auth_provider": "google" if has_google else "password",
                }
            )
            return {"changes": 1}

        if sql.startswith("UPDATE users SET google_sub"):
            # Two shapes: the email auto-link also revokes the password;
            # the explicit link from a signed-in account keeps it.
            revokes_password = "password = ?" in sql
            if revokes_password:
                google_sub, email, password, uid = params
            else:
                google_sub, email, uid = params
            if any(u.get("google_sub") == google_sub for u in self.users):
                raise RuntimeError("D1 error: UNIQUE constraint failed: users.google_sub")
            for u in self.users:
                # Mirrors "WHERE id = ? AND google_sub IS NULL"
                if u["id"] == uid and u.get("google_sub") is None:
                    u.update(google_sub=google_sub, email=email)
                    if revokes_password:
                        u.update(password=password, auth_provider="google")
                    return {"changes": 1}
            return {"changes": 0}

        return {"changes": 0}


@pytest.fixture
def d1(monkeypatch):
    fake = FakeD1()
    monkeypatch.setattr(user_api, "get_d1_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    """Plaintext fallback off, Google and JWT configured — unless a test says otherwise."""
    monkeypatch.setattr(settings, "ALLOW_PLAINTEXT_LOGIN", False, raising=False)
    monkeypatch.setattr(
        settings, "GOOGLE_CLIENT_ID", "test.apps.googleusercontent.com", raising=False
    )
    monkeypatch.setattr(
        settings, "JWT_SECRET", "test-secret-long-enough-for-the-policy", raising=False
    )
    monkeypatch.setattr(settings, "JWT_EXPIRY_DAYS", 30, raising=False)


def _stub_google(monkeypatch, identity=None, error=None):
    def _verify(token, client_id):
        if error:
            raise error
        return identity

    monkeypatch.setattr(user_api, "verify_google_id_token", _verify)


def register(username, password):
    return _run(
        user_api.register_user(
            UserRegisterRequest(username=username, password=password)
        )
    )


def login(username, password):
    return _run(
        user_api.user_login(UserLoginRequest(username=username, password=password))
    )


def google_sign_in(token="tok"):
    return _run(user_api.google_sign_in(GoogleSignInRequest(idToken=token)))


def _legacy_row(username="legacy", password="hunter2", google_sub=None):
    return {
        "id": "u1",
        "username": username,
        "password": password,
        "google_sub": google_sub,
        "auth_provider": "google" if google_sub else "password",
    }


# ── /register ────────────────────────────────────────────────────────────────

def test_register_stores_a_bcrypt_hash_not_the_password(d1):
    assert register("akshat", "s3cret!")["status"] == "success"
    stored = d1.users[0]["password"]
    assert stored.startswith("$2")
    assert "s3cret!" not in stored


def test_register_rejects_short_password(d1):
    with pytest.raises(HTTPException) as exc:
        register("akshat", "abc")
    assert exc.value.status_code == 400
    assert d1.users == []


def test_register_rejects_email_shaped_username(d1):
    """Closes the pre-registration squat on the Google namespace."""
    with pytest.raises(HTTPException) as exc:
        register("victim@gmail.com", "s3cret!")
    assert exc.value.status_code == 400
    assert "Google" in exc.value.detail
    assert d1.users == []


def test_register_rejects_duplicate_username(d1):
    register("akshat", "s3cret!")
    with pytest.raises(HTTPException) as exc:
        register("akshat", "other1")
    assert exc.value.status_code == 400
    assert len(d1.users) == 1


def test_register_error_does_not_leak_internals(d1, monkeypatch):
    async def boom(sql, params=None):
        raise RuntimeError("D1 error: postgres://user:pw@host")

    monkeypatch.setattr(d1, "execute", boom)
    with pytest.raises(HTTPException) as exc:
        register("akshat", "s3cret!")
    assert exc.value.status_code == 500
    assert exc.value.detail == "Registration failed"


# ── /userLogin ───────────────────────────────────────────────────────────────

def test_login_succeeds_against_a_hashed_password(d1):
    register("akshat", "s3cret!")
    assert login("akshat", "s3cret!")["status"] == "success"


def test_login_rejects_wrong_password(d1):
    register("akshat", "s3cret!")
    with pytest.raises(HTTPException) as exc:
        login("akshat", "wrong!")
    assert exc.value.status_code == 401


def test_login_of_unknown_user_matches_wrong_password_response(d1):
    """Same status and message either way — no account enumeration."""
    register("akshat", "s3cret!")
    with pytest.raises(HTTPException) as wrong_pw:
        login("akshat", "wrong!")
    with pytest.raises(HTTPException) as no_user:
        login("nobody", "wrong!")
    assert wrong_pw.value.status_code == no_user.value.status_code == 401
    assert wrong_pw.value.detail == no_user.value.detail


def test_unmigrated_plaintext_row_cannot_log_in_by_default(d1):
    """The point of failing closed: plaintext rows are dead until migrated."""
    d1.users.append(_legacy_row())
    with pytest.raises(HTTPException) as exc:
        login("legacy", "hunter2")
    assert exc.value.status_code == 401


def test_plaintext_row_can_log_in_during_the_migration_window(d1, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PLAINTEXT_LOGIN", True, raising=False)
    d1.users.append(_legacy_row())
    assert login("legacy", "hunter2")["status"] == "success"
    with pytest.raises(HTTPException):
        login("legacy", "nope")


def test_google_only_account_cannot_be_password_logged_in(d1, monkeypatch):
    """Even with the migration gate open, the sentinel must never authenticate."""
    monkeypatch.setattr(settings, "ALLOW_PLAINTEXT_LOGIN", True, raising=False)
    d1.users.append(
        _legacy_row("reader@gmail.com", NO_PASSWORD_SENTINEL, google_sub="sub-1")
    )
    with pytest.raises(HTTPException) as exc:
        login("reader@gmail.com", NO_PASSWORD_SENTINEL)
    assert exc.value.status_code == 401


# ── /auth/google ─────────────────────────────────────────────────────────────

def test_google_sign_in_creates_an_account_keyed_on_the_email(d1, monkeypatch):
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="reader@gmail.com"))
    assert google_sign_in()["username"] == "reader@gmail.com"
    created = d1.users[0]
    assert created["google_sub"] == "sub-1"
    assert created["password"] == NO_PASSWORD_SENTINEL
    assert created["auth_provider"] == "google"


def test_repeat_google_sign_in_reuses_the_same_account(d1, monkeypatch):
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="reader@gmail.com"))
    assert google_sign_in()["username"] == google_sign_in()["username"]
    assert len(d1.users) == 1


def test_google_sign_in_links_to_existing_username_and_revokes_its_password(
    d1, monkeypatch
):
    d1.users.append(_legacy_row("reader@gmail.com", hash_password("legacy-pw")))
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="reader@gmail.com"))

    assert google_sign_in()["username"] == "reader@gmail.com"
    assert len(d1.users) == 1, "must link, not create a second account"
    linked = d1.users[0]
    assert linked["google_sub"] == "sub-1"
    assert linked["password"] == NO_PASSWORD_SENTINEL

    # The legacy credential no longer works.
    with pytest.raises(HTTPException):
        login("reader@gmail.com", "legacy-pw")


def test_google_sign_in_rejects_a_row_already_linked_to_someone_else(d1, monkeypatch):
    d1.users.append(
        _legacy_row("reader@gmail.com", NO_PASSWORD_SENTINEL, google_sub="other-sub")
    )
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="reader@gmail.com"))
    with pytest.raises(HTTPException) as exc:
        google_sign_in()
    assert exc.value.status_code == 409
    assert d1.users[0]["google_sub"] == "other-sub"


def test_invalid_token_is_rejected_generically(d1, monkeypatch):
    _stub_google(monkeypatch, error=GoogleIdentityError("Google email is not verified"))
    with pytest.raises(HTTPException) as exc:
        google_sign_in()
    assert exc.value.status_code == 401
    # No internal reason echoed back to the caller.
    assert exc.value.detail == "Could not verify Google sign-in"


def test_google_sign_in_is_unavailable_when_client_id_is_unset(d1, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None, raising=False)
    with pytest.raises(HTTPException) as exc:
        google_sign_in()
    assert exc.value.status_code == 503


# ── Tokens issued on sign-in ─────────────────────────────────────────────────

def test_login_issues_a_token_for_the_authenticated_user(d1):
    register("akshat", "s3cret!")
    body = login("akshat", "s3cret!")

    assert body["token_type"] == "bearer"
    claims = decode_access_token(body["access_token"])
    assert claims.username == "akshat"
    assert claims.user_id == d1.users[0]["id"]


def test_google_sign_in_issues_a_token(d1, monkeypatch):
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="reader@gmail.com"))
    body = google_sign_in()

    claims = decode_access_token(body["access_token"])
    assert claims.username == "reader@gmail.com"
    # The token's subject must be the real row id, not a placeholder.
    assert claims.user_id == d1.users[0]["id"]


def test_login_response_never_contains_the_password_hash(d1):
    """The session payload is built field-by-field, not spread from the row."""
    register("akshat", "s3cret!")
    body = login("akshat", "s3cret!")
    assert "password" not in body
    assert not any(
        isinstance(v, str) and v.startswith("$2") for v in body.values()
    )


# ── Identity comes from the token, not from a parameter ──────────────────────

def _auth_as(username: str, user_id: str = "u-caller") -> AuthenticatedUser:
    return AuthenticatedUser(id=user_id, username=username)


def _seed_two_users(d1):
    d1.users.append(_legacy_row("akshat", hash_password("s3cret!")))
    d1.users[-1]["id"] = "u-akshat"
    d1.users.append(_legacy_row("victim", hash_password("other-pw")))
    d1.users[-1]["id"] = "u-victim"


def _patch_progress_io(d1, monkeypatch, recorded):
    """Record the user_id each progress statement runs against."""

    async def fake_execute(sql, params=None):
        recorded["write_user_id"] = (params or [None])[0]
        return {"changes": 1}

    async def fake_query(sql, params=None):
        if "FROM users" in sql:
            return [dict(u) for u in d1.users if u["username"] == (params or [None])[0]]
        recorded["read_user_id"] = (params or [None])[0]
        return [
            {
                "novel_id": "novel-1",
                "chapter_number": 5,
                "updated_at": "2026-01-01",
            }
        ]

    async def fake_resolve_novel_id(_d1, novel_name):
        return "novel-1"

    monkeypatch.setattr(d1, "execute", fake_execute)
    monkeypatch.setattr(d1, "query", fake_query)
    monkeypatch.setattr(user_api, "resolve_novel_id", fake_resolve_novel_id)


def test_progress_request_no_longer_carries_a_username(d1):
    """The regression guard for this whole change.

    A crafted body naming someone else must not even be representable —
    pydantic drops the unknown field, so the handler can never see it.
    """
    crafted = UserProgressRequest(
        username="victim", novelName="n", lastChapterRead=5
    )
    assert not hasattr(crafted, "username")
    assert "username" not in crafted.model_dump()


def test_progress_write_uses_the_token_user_not_a_parameter(d1, monkeypatch):
    _seed_two_users(d1)
    recorded = {}
    _patch_progress_io(d1, monkeypatch, recorded)

    _run(
        user_api.save_user_progress(
            UserProgressRequest(novelName="novel-1", lastChapterRead=5),
            caller=_auth_as("akshat"),
        )
    )
    assert recorded["write_user_id"] == "u-akshat", "wrote against the wrong user"


def test_progress_read_uses_the_token_user(d1, monkeypatch):
    _seed_two_users(d1)
    recorded = {}
    _patch_progress_io(d1, monkeypatch, recorded)

    _run(user_api.get_all_user_progress(caller=_auth_as("akshat")))
    assert recorded["read_user_id"] == "u-akshat"


def test_a_token_for_a_deleted_account_is_rejected(d1):
    """Claims are not a free pass: the row still has to exist for writes."""
    with pytest.raises(HTTPException) as exc:
        _run(user_api.get_all_user_progress(caller=_auth_as("ghost")))
    assert exc.value.status_code == 401


# ── Linking Google to a signed-in account ────────────────────────────────────

def link_google(caller, token="tok"):
    return _run(
        user_api.link_google_account(GoogleSignInRequest(idToken=token), caller=caller)
    )


def test_link_attaches_google_to_the_callers_account_and_keeps_the_password(
    d1, monkeypatch
):
    register("akshat", "s3cret!")
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="me@gmail.com"))

    body = link_google(_auth_as("akshat"))

    assert body["linked"] is True
    row = d1.users[0]
    assert row["google_sub"] == "sub-1"
    assert row["email"] == "me@gmail.com"
    # The owner proved themselves with the token, so the password still works.
    assert login("akshat", "s3cret!")["status"] == "success"


def test_after_linking_google_sign_in_reaches_the_existing_account(d1, monkeypatch):
    """The point of the feature: Google lands on the existing library."""
    register("akshat", "s3cret!")
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="me@gmail.com"))
    link_google(_auth_as("akshat"))

    body = google_sign_in()

    assert body["username"] == "akshat"
    assert len(d1.users) == 1, "must not create a separate Gmail-named account"


def test_link_route_requires_a_signed_in_caller():
    """Without a token there is no proof of owning any account."""
    import inspect

    caller_param = inspect.signature(user_api.link_google_account).parameters["caller"]
    assert caller_param.default.dependency is get_current_user


def test_relinking_the_same_google_account_is_a_no_op(d1, monkeypatch):
    register("akshat", "s3cret!")
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="me@gmail.com"))
    link_google(_auth_as("akshat"))

    assert link_google(_auth_as("akshat"))["linked"] is True
    assert d1.users[0]["google_sub"] == "sub-1"


def test_link_refuses_a_google_account_already_linked_elsewhere(d1, monkeypatch):
    """A link must never silently take Google sign-in away from another account."""
    register("akshat", "s3cret!")
    register("yoyo", "other-pw")
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="me@gmail.com"))
    link_google(_auth_as("yoyo"))

    with pytest.raises(HTTPException) as exc:
        link_google(_auth_as("akshat"))

    assert exc.value.status_code == 409
    owners = [u["username"] for u in d1.users if u.get("google_sub") == "sub-1"]
    assert owners == ["yoyo"]


def test_link_refuses_to_replace_a_different_google_account(d1, monkeypatch):
    register("akshat", "s3cret!")
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="me@gmail.com"))
    link_google(_auth_as("akshat"))

    _stub_google(monkeypatch, GoogleIdentity(subject="sub-2", email="alt@gmail.com"))
    with pytest.raises(HTTPException) as exc:
        link_google(_auth_as("akshat"))

    assert exc.value.status_code == 409
    assert d1.users[0]["google_sub"] == "sub-1"


def test_link_rejects_an_invalid_google_token(d1, monkeypatch):
    register("akshat", "s3cret!")
    _stub_google(monkeypatch, error=GoogleIdentityError("Google email is not verified"))

    with pytest.raises(HTTPException) as exc:
        link_google(_auth_as("akshat"))

    assert exc.value.status_code == 401
    assert exc.value.detail == "Could not verify Google sign-in"
    assert d1.users[0].get("google_sub") is None


def test_link_with_a_token_for_a_deleted_account_is_rejected(d1, monkeypatch):
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="me@gmail.com"))
    with pytest.raises(HTTPException) as exc:
        link_google(_auth_as("ghost"))
    assert exc.value.status_code == 401


def test_link_is_unavailable_when_google_is_not_configured(d1, monkeypatch):
    register("akshat", "s3cret!")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None, raising=False)
    with pytest.raises(HTTPException) as exc:
        link_google(_auth_as("akshat"))
    assert exc.value.status_code == 503


# ── The dependency itself ────────────────────────────────────────────────────

class _FakeRequest:
    url = type("U", (), {"path": "/user/progress"})()


def test_missing_authorization_header_is_401_not_403(d1):
    """FastAPI's HTTPBearer defaults to 403; both clients key logout off 401."""
    with pytest.raises(HTTPException) as exc:
        _run(get_current_user(_FakeRequest(), credentials=None))
    assert exc.value.status_code == 401


def test_invalid_token_is_401(d1):
    from fastapi.security import HTTPAuthorizationCredentials

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="garbage")
    with pytest.raises(HTTPException) as exc:
        _run(get_current_user(_FakeRequest(), credentials=creds))
    assert exc.value.status_code == 401
    # The reason is logged, not returned.
    assert exc.value.detail == "Not authenticated"


def test_valid_token_resolves_to_its_subject(d1):
    register("akshat", "s3cret!")
    from fastapi.security import HTTPAuthorizationCredentials

    token = login("akshat", "s3cret!")["access_token"]
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    caller = _run(get_current_user(_FakeRequest(), credentials=creds))
    assert caller.username == "akshat"


def test_no_success_response_contains_a_credential(d1, monkeypatch):
    """Sweep the auth surface for credential echo."""
    _stub_google(monkeypatch, GoogleIdentity(subject="sub-1", email="reader@gmail.com"))
    bodies = [
        json.dumps(register("akshat", "s3cret!")),
        json.dumps(login("akshat", "s3cret!")),
        json.dumps(google_sign_in("tok-abc")),
    ]
    for body in bodies:
        assert "s3cret!" not in body
        assert "tok-abc" not in body
        assert not re.search(r"\$2[aby]\$", body), "bcrypt hash leaked into a response"
