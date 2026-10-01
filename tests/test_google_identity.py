"""Unit tests for app/core/google_identity.py claims mapping.

Only the pure `identity_from_claims` / helpers are covered — the network-facing
`verify_google_id_token` delegates signature and audience checks to google-auth.

Run: python -m pytest tests/test_google_identity.py
"""

import pytest

from app.core.google_identity import (
    GoogleIdentity,
    GoogleIdentityError,
    google_username,
    identity_from_claims,
    looks_like_email,
)


def _claims(**overrides) -> dict:
    base = {
        "iss": "https://accounts.google.com",
        "sub": "1234567890",
        "email": "reader@gmail.com",
        "email_verified": True,
    }
    base.update(overrides)
    return base


def test_maps_verified_claims_to_identity():
    identity = identity_from_claims(_claims())
    assert identity == GoogleIdentity(subject="1234567890", email="reader@gmail.com")


def test_accepts_bare_issuer_form():
    assert identity_from_claims(_claims(iss="accounts.google.com")).subject == "1234567890"


def test_email_is_normalised():
    assert identity_from_claims(_claims(email="  Reader@GMail.com ")).email == "reader@gmail.com"


def test_unverified_email_is_rejected():
    """The email is the account key — an unverified one would allow takeover."""
    with pytest.raises(GoogleIdentityError):
        identity_from_claims(_claims(email_verified=False))


def test_missing_email_verified_claim_is_rejected():
    claims = _claims()
    del claims["email_verified"]
    with pytest.raises(GoogleIdentityError):
        identity_from_claims(claims)


def test_string_true_email_verified_is_accepted():
    """Google has historically serialised this claim as a string."""
    assert identity_from_claims(_claims(email_verified="true")).email == "reader@gmail.com"


def test_unexpected_issuer_is_rejected():
    with pytest.raises(GoogleIdentityError):
        identity_from_claims(_claims(iss="https://evil.example.com"))


def test_missing_subject_is_rejected():
    claims = _claims()
    del claims["sub"]
    with pytest.raises(GoogleIdentityError):
        identity_from_claims(claims)


def test_missing_email_is_rejected():
    claims = _claims()
    del claims["email"]
    with pytest.raises(GoogleIdentityError):
        identity_from_claims(claims)


def test_google_username_is_the_email():
    assert google_username(GoogleIdentity(subject="s", email="a@b.com")) == "a@b.com"


def test_looks_like_email_guards_the_google_namespace():
    assert looks_like_email("someone@gmail.com") is True
    assert looks_like_email("akshat") is False
    assert looks_like_email("") is False
    assert looks_like_email(None) is False
