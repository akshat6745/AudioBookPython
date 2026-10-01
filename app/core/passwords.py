"""Password hashing helpers.

Deliberately pure — no D1, FastAPI or settings imports — so it is directly
unit-testable (see tests/test_passwords.py) and cheap to reason about.

Hashing is bcrypt. Callers must run these off the event loop (see
`app/api/user.py`): at cost 12 a single hash is hundreds of milliseconds of
synchronous CPU, which would otherwise stall every concurrent request.
"""

import secrets

import bcrypt

BCRYPT_ROUNDS = 12

# bcrypt hashes at most 72 bytes of input. It raises rather than silently
# truncating, so we surface that as a validation error instead of a 500.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 6

# Stored in `users.password` for accounts that authenticate through Google
# only. It is not a bcrypt hash, so `verify_password` can never accept it —
# such a row cannot be logged into with a password.
NO_PASSWORD_SENTINEL = "!nopassword"


class PasswordPolicyError(ValueError):
    """Raised when a new password fails the length policy."""


def hash_password(plaintext: str) -> str:
    """Hash a *new* password, enforcing the length policy.

    Raises PasswordPolicyError if too short (characters) or too long (bytes).
    """
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    encoded = plaintext.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(BCRYPT_ROUNDS)).decode("utf-8")


def is_hashed(stored: object) -> bool:
    """Whether a stored value is a bcrypt hash we can verify against."""
    return isinstance(stored, str) and stored.startswith("$2")


def verify_password(plaintext: str, stored: object) -> bool:
    """Verify a password against a stored bcrypt hash.

    Fails closed: anything that is not a bcrypt hash — a legacy plaintext row
    that was never migrated, the Google-only sentinel, NULL — returns False
    rather than falling back to a weaker comparison.
    """
    if not is_hashed(stored):
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), stored.encode("utf-8"))
    except ValueError:
        # Over-length input, or a malformed/truncated stored hash.
        return False


def verify_legacy_plaintext(plaintext: str, stored: object) -> bool:
    """Constant-time comparison against a pre-migration plaintext password.

    TEMPORARY. Exists only so logins keep working during the window between
    deploying hash verification and running scripts/hash_existing_passwords.py.
    Reachable only when ALLOW_PLAINTEXT_LOGIN is explicitly enabled; delete
    this function and its call site once the migration has run.
    """
    if not isinstance(stored, str) or is_hashed(stored):
        return False
    # The Google-only sentinel is a literal string, so a plaintext comparison
    # would otherwise let anyone in by simply submitting it as the password.
    if stored == NO_PASSWORD_SENTINEL:
        return False
    return secrets.compare_digest(plaintext, stored)
