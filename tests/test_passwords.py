"""Unit tests for app/core/passwords.py — pure, no D1 or network needed.

Run: python -m pytest tests/test_passwords.py
"""

import pytest

from app.core.passwords import (
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_LENGTH,
    NO_PASSWORD_SENTINEL,
    PasswordPolicyError,
    hash_password,
    is_hashed,
    verify_legacy_plaintext,
    verify_password,
)


def test_hash_then_verify_roundtrip():
    stored = hash_password("correct horse")
    assert stored.startswith("$2")
    assert verify_password("correct horse", stored) is True


def test_wrong_password_is_rejected():
    stored = hash_password("correct horse")
    assert verify_password("Correct horse", stored) is False
    assert verify_password("", stored) is False


def test_same_password_hashes_differently_each_time():
    """Distinct salts — two users with the same password must not collide."""
    assert hash_password("same-password") != hash_password("same-password")


def test_legacy_plaintext_stored_value_fails_closed():
    """A row that was never migrated must not be loggable via verify_password."""
    assert verify_password("hunter2", "hunter2") is False


def test_google_only_sentinel_can_never_be_logged_into():
    assert verify_password(NO_PASSWORD_SENTINEL, NO_PASSWORD_SENTINEL) is False
    assert verify_password("", NO_PASSWORD_SENTINEL) is False
    assert verify_password("anything", NO_PASSWORD_SENTINEL) is False


def test_missing_or_malformed_stored_value_fails_closed():
    assert verify_password("x", None) is False
    assert verify_password("x", "") is False
    assert verify_password("x", "$2b$truncated") is False


def test_password_below_minimum_length_is_rejected():
    with pytest.raises(PasswordPolicyError):
        hash_password("a" * (MIN_PASSWORD_LENGTH - 1))


def test_password_over_bcrypt_byte_limit_is_rejected():
    """bcrypt only hashes 72 bytes; reject rather than silently truncate."""
    with pytest.raises(PasswordPolicyError):
        hash_password("a" * (MAX_PASSWORD_BYTES + 1))


def test_byte_limit_counts_bytes_not_characters():
    """Multi-byte characters must count against the bcrypt byte budget."""
    # 30 x 3-byte characters = 90 bytes, but only 30 characters.
    with pytest.raises(PasswordPolicyError):
        hash_password("é€" * 20)


def test_overlong_input_does_not_raise_on_verify():
    """An over-length login attempt is a failed login, not a 500."""
    stored = hash_password("correct horse")
    assert verify_password("a" * 200, stored) is False


def test_is_hashed():
    assert is_hashed(hash_password("abcdef")) is True
    assert is_hashed("plaintext") is False
    assert is_hashed(NO_PASSWORD_SENTINEL) is False
    assert is_hashed(None) is False


class TestLegacyPlaintextFallback:
    """The temporary migration-window path."""

    def test_matches_exact_legacy_value(self):
        assert verify_legacy_plaintext("hunter2", "hunter2") is True

    def test_rejects_mismatch(self):
        assert verify_legacy_plaintext("hunter2", "hunter3") is False

    def test_refuses_to_compare_against_a_hash(self):
        """Must not let a hash string itself be used as a password."""
        stored = hash_password("correct horse")
        assert verify_legacy_plaintext(stored, stored) is False

    def test_refuses_the_google_only_sentinel(self):
        """Submitting the sentinel string must not authenticate a Google account."""
        assert verify_legacy_plaintext(NO_PASSWORD_SENTINEL, NO_PASSWORD_SENTINEL) is False

    def test_rejects_missing_stored_value(self):
        assert verify_legacy_plaintext("x", None) is False
