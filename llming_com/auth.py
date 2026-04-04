"""HMAC-based cookie authentication for llming applications.

Provides signed auth tokens (session-level), identity tokens (user-level),
and cookie verification. Framework-agnostic -- works with any ASGI framework
that exposes ``request.cookies`` (Starlette, FastAPI, etc.).

No external dependencies beyond Python stdlib.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
import warnings
from typing import Optional

logger = logging.getLogger(__name__)

# Cookie names
AUTH_COOKIE_NAME = "llming_auth"
SESSION_COOKIE_NAME = "llming_session"
IDENTITY_COOKIE_NAME = "llming_identity"

# ── Lazy secret loading ──────────────────────────────────────────

_cached_secret: Optional[str] = None


def _get_auth_secret() -> str:
    """Return the auth secret, loading lazily on first call.

    - If ``LLMING_AUTH_SECRET`` env var is set, use it.
    - If ``LLMING_ENV`` is ``"dev"`` or ``"development"``, generate a random
      fallback and log a CRITICAL warning.
    - Otherwise raise ``RuntimeError`` -- production must set the secret.
    """
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret

    env_secret = os.environ.get("LLMING_AUTH_SECRET", "")
    if env_secret:
        _cached_secret = env_secret
        return _cached_secret

    llming_env = os.environ.get("LLMING_ENV", "").lower()
    if llming_env in ("dev", "development"):
        _cached_secret = "llming_dev_" + secrets.token_hex(8)
        logger.critical(
            "LLMING_AUTH_SECRET not set -- using random dev-only secret. "
            "Do NOT use this in production!"
        )
        return _cached_secret

    raise RuntimeError("LLMING_AUTH_SECRET must be set in production")


def _reset_secret() -> None:
    """Reset the cached secret (for testing)."""
    global _cached_secret
    _cached_secret = None


# ── Auth tokens (session-level) ──────────────────────────────────


def sign_auth_token(session_id: str) -> str:
    """Create an HMAC-signed auth token.

    Format: ``<session_id>.<timestamp>.<signature>``

    The timestamp enables expiry checking in ``verify_auth_cookie``.
    """
    secret = _get_auth_secret()
    ts = str(int(time.time()))
    payload = f"{session_id}.{ts}"
    sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{session_id}.{ts}.{sig}"


def verify_auth_cookie(request, *, max_age: int = 86400) -> bool:
    """Verify the ``llming_auth`` cookie on a request.

    Accepts both the new 3-part format (session.timestamp.sig) and legacy
    2-part format (session.sig) with a deprecation warning.

    Args:
        request: Any object with a ``.cookies`` dict (Starlette Request, etc.)
        max_age: Maximum token age in seconds (default 24 h).

    Returns:
        True if the cookie is present and the HMAC signature is valid.
    """
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token or "." not in token:
        return False

    secret = _get_auth_secret()
    parts = token.split(".")

    if len(parts) == 3:
        session_id, ts_str, sig = parts
        try:
            ts = int(ts_str)
        except ValueError:
            return False
        if time.time() - ts > max_age:
            return False
        payload = f"{session_id}.{ts_str}"
        expected = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    if len(parts) == 2:
        # Legacy 2-part token (no timestamp) -- accept with deprecation warning
        warnings.warn(
            "Legacy 2-part auth token detected. Migrate to 3-part tokens.",
            DeprecationWarning,
            stacklevel=2,
        )
        session_id, sig = parts
        expected = hmac.new(
            secret.encode(), session_id.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    return False


def get_auth_session_id(request, *, max_age: int = 86400) -> Optional[str]:
    """Extract the session_id from a valid ``llming_auth`` cookie.

    Returns None if the cookie is missing or invalid.
    """
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token or "." not in token:
        return None

    secret = _get_auth_secret()
    parts = token.split(".")

    if len(parts) == 3:
        session_id, ts_str, sig = parts
        try:
            ts = int(ts_str)
        except ValueError:
            return None
        if time.time() - ts > max_age:
            return None
        payload = f"{session_id}.{ts_str}"
        expected = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(sig, expected):
            return session_id
        return None

    if len(parts) == 2:
        # Legacy 2-part
        warnings.warn(
            "Legacy 2-part auth token detected. Migrate to 3-part tokens.",
            DeprecationWarning,
            stacklevel=2,
        )
        session_id, sig = parts
        expected = hmac.new(
            secret.encode(), session_id.encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(sig, expected):
            return session_id
        return None

    return None


# ── Identity tokens (user-level) ─────────────────────────────────


def sign_identity_token(identity_id: str) -> str:
    """Create an HMAC-signed identity token from a user/OAuth session ID.

    The token contains the identity_id and a timestamp, used to key external
    storage (e.g. Redis). Format: ``<identity_id>.<timestamp>.<signature>``.
    """
    secret = _get_auth_secret()
    ts = int(time.time())
    payload = f"id:{identity_id}:{ts}"
    sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
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

    secret = _get_auth_secret()
    parts = token.split(".")

    if len(parts) == 2:
        # Legacy format (no timestamp) -- accept during transition period
        identity_id, sig = parts
        expected = hmac.new(
            secret.encode(), f"id:{identity_id}".encode(), hashlib.sha256
        ).hexdigest()
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
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    if hmac.compare_digest(sig, expected):
        return identity_id
    return None


# ── Cookie factory ────────────────────────────────────────────────


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
