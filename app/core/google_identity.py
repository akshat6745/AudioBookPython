"""Google Sign-In ID token verification.

Two layers, split so the decision logic is testable without network access:

- `verify_google_id_token` — thin wrapper over google-auth's official verifier,
  which handles signature, issuer, audience and expiry checks and fetches
  Google's signing certs itself. We do not hand-roll JWT/JWKS handling.
- `identity_from_claims` — pure mapping from already-verified claims to the
  identity we store. Unit-tested in tests/test_google_identity.py.
"""

from dataclasses import dataclass
from typing import Optional

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

# Issuers Google mints ID tokens under. google-auth checks this itself; we
# keep the tuple for the claims-level assertion in identity_from_claims.
_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleIdentityError(ValueError):
    """Raised when an ID token or its claims cannot be trusted."""


@dataclass(frozen=True)
class GoogleIdentity:
    """A verified Google identity.

    `subject` is Google's stable per-user id and is the durable key. `email`
    is the display/login handle but is NOT stable — it changes with Workspace
    renames, and dot/plus aliases of one mailbox yield different strings.
    """

    subject: str
    email: str


def identity_from_claims(claims: dict) -> GoogleIdentity:
    """Map verified ID token claims to a GoogleIdentity.

    Assumes the signature/audience were already checked by google-auth; this
    enforces the claim-level requirements we care about.
    """
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise GoogleIdentityError("Unexpected token issuer")

    subject = claims.get("sub")
    if not subject:
        raise GoogleIdentityError("Token has no subject")

    email = claims.get("email")
    if not email:
        raise GoogleIdentityError("Token has no email")

    # An unverified email must never be trusted: it is what we key the account
    # on, so accepting it would let anyone claim another user's address.
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleIdentityError("Google email is not verified")

    return GoogleIdentity(subject=subject, email=email.strip().lower())


def verify_google_id_token(token: str, client_id: str) -> GoogleIdentity:
    """Verify a Google ID token and return the identity it attests to.

    `client_id` is the Web OAuth client ID — the audience Android mints the
    token for. Raises GoogleIdentityError if the token is not trustworthy.
    """
    try:
        claims = id_token.verify_oauth2_token(
            token, google_requests.Request(), client_id
        )
    except Exception as exc:  # google-auth raises several unrelated types
        raise GoogleIdentityError("Invalid Google ID token") from exc
    return identity_from_claims(claims)


def google_username(identity: GoogleIdentity) -> str:
    """The `users.username` value for a Google identity."""
    return identity.email


def looks_like_email(username: Optional[str]) -> bool:
    """Whether a username occupies the email namespace Google sign-in owns.

    Used to keep new password registrations out of that namespace, so nobody
    can pre-register someone else's email and be auto-linked into later.
    """
    return bool(username) and "@" in username
