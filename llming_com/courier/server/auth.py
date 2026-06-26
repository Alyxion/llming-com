"""Upload authentication — ``Authorization: Bearer <api-key>`` (§5.4).

The key authorises *upload only*; it never appears in a URL or query string,
so it stays out of access logs and history. Downloads need no credential.
When no keys are configured the server runs open — intended for local
development only, and logged as such by the caller.
"""

from __future__ import annotations

from ..config import Settings
from ..exceptions import UnauthorizedError


def check_bearer(authorization: str | None, settings: Settings) -> None:
    """Validate an ``Authorization`` header value; raise on failure.

    No configured keys ⇒ auth disabled (dev mode). Otherwise the header must
    be ``Bearer <key>`` with ``<key>`` in the configured set.
    """
    if not settings.api_keys:
        return  # dev mode: auth disabled
    if not authorization:
        raise UnauthorizedError("missing Authorization header")
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not key:
        raise UnauthorizedError("expected 'Authorization: Bearer <key>'")
    if key not in settings.api_keys:
        raise UnauthorizedError("invalid api key")
