"""Exception hierarchy for the exchange.

Each error carries an HTTP-ish ``status`` so the FastAPI dev server and the
Azure Function host can translate failures into responses uniformly without
re-deriving the mapping.
"""

from __future__ import annotations


class CourierError(Exception):
    """Base class for all exchange errors."""

    status: int = 500
    code: str = "courier_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.__doc__ or self.code)


class UnauthorizedError(CourierError):
    """The upload bearer key was missing or invalid."""

    status = 401
    code = "unauthorized"


class PayloadTooLargeError(CourierError):
    """The uploaded body exceeded the configured maximum size."""

    status = 413
    code = "payload_too_large"


class TTLExceededError(CourierError):
    """The requested TTL exceeded the configured hard maximum."""

    status = 400
    code = "ttl_exceeded"


class NotFoundError(CourierError):
    """No live object exists for the given id (missing, expired, or consumed)."""

    status = 404
    code = "not_found"


class ForbiddenError(CourierError):
    """The download capability (SAS token) was invalid or expired."""

    status = 403
    code = "forbidden"


class IntegrityError(CourierError):
    """Decryption or the plaintext SHA-256 check failed — the payload is corrupt."""

    status = 422
    code = "integrity_error"


class ValidationError(CourierError):
    """A request parameter (ttl, content-type, …) was malformed."""

    status = 400
    code = "validation_error"
