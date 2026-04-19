"""HMAC-based cookie authentication for llming applications.

Provides signed auth tokens (session-level), identity tokens (user-level),
and cookie verification. Framework-agnostic -- works with any ASGI framework
that exposes ``request.cookies`` (Starlette, FastAPI, etc.).

Usage::

    auth = AuthManager()  # reads LLMING_AUTH_SECRET from env
    token = auth.sign_auth_token("session-123")
    assert auth.verify_auth_cookie(request)

No external dependencies beyond Python stdlib.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
import warnings
from typing import Optional

logger = logging.getLogger(__name__)

# Default cookie names (used when no app_name is specified)
AUTH_COOKIE_NAME = "llming_auth"
SESSION_COOKIE_NAME = "llming_session"
IDENTITY_COOKIE_NAME = "llming_identity"


class AuthManager:
    """HMAC-based cookie auth manager.

    Encapsulates the auth secret and all signing/verification logic.
    Use the module-level ``default()`` function to get the shared instance.

    Each app should use its own ``app_name`` to avoid cookie collisions
    when multiple llming-com apps run on the same domain::

        auth = AuthManager(app_name="openhort")
        # cookies: openhort_auth, openhort_session, openhort_identity

        auth = AuthManager()  # default: llming_auth, llming_session, llming_identity
    """

    def __init__(self, secret: str | None = None, *, app_name: str = ""):
        """Create an AuthManager.

        Args:
            secret: HMAC secret. If ``None``, reads ``LLMING_AUTH_SECRET``
                from the environment (lazy, on first use).
            app_name: App-specific prefix for cookie names. If empty,
                uses the default ``llming_*`` names.
        """
        self._secret: str | None = secret
        prefix = app_name.replace("-", "_") if app_name else "llming"
        self.auth_cookie_name = f"{prefix}_auth"
        self.session_cookie_name = f"{prefix}_session"
        self.identity_cookie_name = f"{prefix}_identity"

    @property
    def secret(self) -> str:
        """Return the HMAC secret, resolving from env on first access."""
        if self._secret is not None:
            return self._secret
        env_secret = os.environ.get("LLMING_AUTH_SECRET", "")
        if env_secret:
            self._secret = env_secret
            return self._secret
        llming_env = os.environ.get("LLMING_ENV", "").lower()
        if llming_env in ("dev", "development"):
            self._secret = "llming_dev_" + secrets.token_hex(8)
            logger.critical(
                "LLMING_AUTH_SECRET not set -- using random dev-only secret. "
                "Do NOT use this in production!"
            )
            return self._secret
        raise RuntimeError("LLMING_AUTH_SECRET must be set in production")

    # ── Auth tokens (session-level) ─────────────────────────────

    def sign_auth_token(self, session_id: str) -> str:
        """Create an HMAC-signed auth token.

        Format: ``<session_id>.<timestamp>.<signature>``
        """
        ts = str(int(time.time()))
        payload = f"{session_id}.{ts}"
        sig = hmac.new(
            self.secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"{session_id}.{ts}.{sig}"

    def verify_auth_cookie(self, request, *, max_age: int = 86400) -> bool:
        """Verify the ``llming_auth`` cookie on a request.

        Returns True if the cookie is present and the HMAC signature is valid.
        """
        token = request.cookies.get(self.auth_cookie_name)
        if not token or "." not in token:
            return False

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
                self.secret.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(sig, expected)

        if len(parts) == 2:
            warnings.warn(
                "Legacy 2-part auth token detected. Migrate to 3-part tokens.",
                DeprecationWarning,
                stacklevel=2,
            )
            session_id, sig = parts
            expected = hmac.new(
                self.secret.encode(), session_id.encode(), hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(sig, expected)

        return False

    def get_auth_session_id(self, request, *, max_age: int = 86400) -> Optional[str]:
        """Extract the session_id from a valid ``llming_auth`` cookie.

        Returns None if the cookie is missing or invalid.
        """
        token = request.cookies.get(self.auth_cookie_name)
        if not token or "." not in token:
            return None

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
                self.secret.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(sig, expected):
                return session_id
            return None

        if len(parts) == 2:
            warnings.warn(
                "Legacy 2-part auth token detected. Migrate to 3-part tokens.",
                DeprecationWarning,
                stacklevel=2,
            )
            session_id, sig = parts
            expected = hmac.new(
                self.secret.encode(), session_id.encode(), hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(sig, expected):
                return session_id
            return None

        return None

    # ── Identity tokens (user-level) ────────────────────────────

    def sign_identity_token(self, identity_id: str) -> str:
        """Create an HMAC-signed identity token.

        Format: ``<identity_id>.<timestamp>.<signature>``
        """
        ts = int(time.time())
        payload = f"id:{identity_id}:{ts}"
        sig = hmac.new(
            self.secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"{identity_id}.{ts}.{sig}"

    def verify_identity_cookie(self, request, max_age: int = 2592000) -> Optional[str]:
        """Extract and verify the ``llming_identity`` cookie.

        Returns the identity_id if valid and not expired, None otherwise.
        """
        token = request.cookies.get(self.identity_cookie_name)
        if not token or "." not in token:
            return None

        parts = token.split(".")

        if len(parts) == 2:
            identity_id, sig = parts
            expected = hmac.new(
                self.secret.encode(), f"id:{identity_id}".encode(), hashlib.sha256
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
            self.secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(sig, expected):
            return identity_id
        return None

    # ── Cookie factory ──────────────────────────────────────────

    def make_auth_cookie_value(self) -> tuple[str, str]:
        """Generate a fresh ``(session_id, signed_token)`` pair."""
        session_id = secrets.token_hex(16)
        return session_id, self.sign_auth_token(session_id)


# ── Singleton ───────────────────────────────────────────────────

_default_instance: AuthManager | None = None


def get_auth() -> AuthManager:
    """Return the shared AuthManager instance (lazy-created)."""
    global _default_instance
    if _default_instance is None:
        _default_instance = AuthManager()
    return _default_instance


def _reset_default() -> None:
    """Reset the default instance (for testing)."""
    global _default_instance
    _default_instance = None


# Legacy aliases for tests
_get_auth_secret = lambda: get_auth().secret
_reset_secret = _reset_default
