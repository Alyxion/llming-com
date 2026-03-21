"""HMAC-based cookie authentication for llming applications.

Provides signed auth tokens (session-level), identity tokens (user-level),
and cookie verification. Framework-agnostic — works with any ASGI framework
that exposes ``request.cookies`` (Starlette, FastAPI, etc.).

No external dependencies beyond Python stdlib.
"""

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

# Cookie names
AUTH_COOKIE_NAME = "llming_auth"
SESSION_COOKIE_NAME = "llming_session"
IDENTITY_COOKIE_NAME = "llming_identity"

# Shared secret — MUST be set via LLMING_AUTH_SECRET in production
_AUTH_SECRET = os.environ.get("LLMING_AUTH_SECRET", "")
if not _AUTH_SECRET:
    import warnings
    _AUTH_SECRET = "llming_auth:dev_only:" + os.environ.get("HOSTNAME", "local")
    warnings.warn(
        "LLMING_AUTH_SECRET not set — using insecure dev-only secret. "
        "Set LLMING_AUTH_SECRET in production!",
        stacklevel=1,
    )


def sign_auth_token(session_id: str) -> str:
    """Create an HMAC-signed auth token: ``<session_id>.<signature>``."""
    sig = hmac.new(
        _AUTH_SECRET.encode(), session_id.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{session_id}.{sig}"


def verify_auth_cookie(request) -> bool:
    """Verify the ``llming_auth`` cookie on a request.

    Args:
        request: Any object with a ``.cookies`` dict (Starlette Request, etc.)

    Returns:
        True if the cookie is present and the HMAC signature is valid.
    """
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token or "." not in token:
        return False
    session_id, sig = token.rsplit(".", 1)
    expected = hmac.new(
        _AUTH_SECRET.encode(), session_id.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def get_auth_session_id(request) -> Optional[str]:
    """Extract the session_id from a valid ``llming_auth`` cookie.

    Returns None if the cookie is missing or invalid.
    """
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token or "." not in token:
        return None
    session_id, sig = token.rsplit(".", 1)
    expected = hmac.new(
        _AUTH_SECRET.encode(), session_id.encode(), hashlib.sha256
    ).hexdigest()[:32]
    if hmac.compare_digest(sig, expected):
        return session_id
    return None


def sign_identity_token(identity_id: str) -> str:
    """Create an HMAC-signed identity token from a user/OAuth session ID.

    The token contains the identity_id and a timestamp, used to key external
    storage (e.g. Redis). Format: ``<identity_id>.<timestamp>.<signature>``.
    """
    ts = int(time.time())
    payload = f"id:{identity_id}:{ts}"
    sig = hmac.new(
        _AUTH_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{identity_id}.{ts}.{sig}"


def verify_identity_cookie(request, max_age: int = 604800) -> Optional[str]:
    """Extract and verify the ``llming_identity`` cookie.

    Returns the identity_id if valid and not expired, None otherwise.
    Accepts both legacy 2-part tokens and new 3-part timestamped tokens.

    Args:
        request: Any object with a ``.cookies`` dict.
        max_age: Maximum age in seconds (default 7 days).
    """
    token = request.cookies.get(IDENTITY_COOKIE_NAME)
    if not token or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) == 2:
        # Legacy format (no timestamp) — accept during transition period
        identity_id, sig = parts
        expected = hmac.new(
            _AUTH_SECRET.encode(), f"id:{identity_id}".encode(), hashlib.sha256
        ).hexdigest()[:32]
        if hmac.compare_digest(sig, expected):
            return identity_id
        return None
    if len(parts) != 3:
        return None
    identity_id, ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    if time.time() - ts > max_age:
        return None
    payload = f"id:{identity_id}:{ts}"
    expected = hmac.new(
        _AUTH_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]
    if hmac.compare_digest(sig, expected):
        return identity_id
    return None


def make_auth_cookie_value() -> tuple[str, str]:
    """Generate a fresh auth cookie ``(session_id, signed_token)``.

    The caller delivers the cookie to the browser via HTTP Set-Cookie
    or JavaScript::

        sid, token = make_auth_cookie_value()
        response.set_cookie("llming_auth", token, path="/", max_age=86400)

    Returns:
        ``(session_id, signed_token)``
    """
    session_id = secrets.token_hex(16)
    return session_id, sign_auth_token(session_id)
