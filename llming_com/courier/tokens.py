"""Server-mediated download tokens — the dev-server SAS equivalent.

Azure mints a real per-object read-only SAS. For the in-memory dev backend
(and for single-use, Function-mediated downloads) we mint an equivalent
capability token: an HMAC over ``objectId || expiry`` that the download route
verifies. Same shape as a SAS — locates + authorises read of exactly one
object until expiry — without any cloud dependency.

The token is the capability (§5.3): anyone holding it can read that one object
until it expires. It is never logged in full (§3.7).
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import parse_qs, urlencode

from .crypto import b64u_encode
from .exceptions import ForbiddenError

#: Query parameter names (kept short, SAS-like).
_EXPIRY_PARAM = "se"  # signed expiry (epoch seconds)
_SIG_PARAM = "sig"  # HMAC signature


def _sign(object_id: str, expiry_epoch: int, secret: str) -> str:
    msg = f"{object_id}:{expiry_epoch}".encode()
    mac = hmac.new(secret.encode(), msg, sha256).digest()
    return b64u_encode(mac)


def mint(object_id: str, expiry: datetime, secret: str) -> str:
    """Return the query string (without ``?``) authorising read until *expiry*."""
    expiry_epoch = int(expiry.timestamp())
    sig = _sign(object_id, expiry_epoch, secret)
    return urlencode({_EXPIRY_PARAM: expiry_epoch, _SIG_PARAM: sig})


def verify(object_id: str, query: str, secret: str, *, now: datetime | None = None) -> None:
    """Validate a download token; raise :class:`ForbiddenError` if invalid/expired."""
    params = parse_qs(query)
    try:
        expiry_epoch = int(params[_EXPIRY_PARAM][0])
        sig = params[_SIG_PARAM][0]
    except (KeyError, IndexError, ValueError):
        raise ForbiddenError("missing or malformed capability token")

    expected = _sign(object_id, expiry_epoch, secret)
    if not hmac.compare_digest(sig, expected):
        raise ForbiddenError("invalid capability signature")

    now = now or datetime.now(timezone.utc)
    if now.timestamp() > expiry_epoch:
        raise ForbiddenError("capability token expired")
