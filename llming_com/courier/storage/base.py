"""The storage backend interface.

A backend stores opaque (typically ciphertext) bytes against a random object
id plus its :class:`~llming_com.courier.models.ObjectMetadata`, enforces TTL
expiry natively, and optionally offers direct presigned downloads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ObjectMetadata


class StorageBackend(ABC):
    """Persist and retrieve opaque payload bytes keyed by object id."""

    @abstractmethod
    def put(self, object_id: str, data: bytes, metadata: ObjectMetadata) -> None:
        """Store *data* with *metadata*; native expiry is set from ``metadata.expiry``."""

    @abstractmethod
    def get(self, object_id: str) -> tuple[bytes, ObjectMetadata]:
        """Return ``(bytes, metadata)``; raise ``NotFoundError`` if absent/expired."""

    @abstractmethod
    def head(self, object_id: str) -> ObjectMetadata:
        """Return metadata only; raise ``NotFoundError`` if absent/expired."""

    @abstractmethod
    def delete(self, object_id: str) -> None:
        """Delete the object if present (idempotent; no error if already gone)."""

    # --- Optional direct-download support --------------------------------

    def supports_direct_sas(self) -> bool:
        """Whether the backend can mint a direct presigned (SAS) download URL.

        When ``False`` (e.g. the in-memory dev backend), downloads are served
        through the Function/dev-server route using an HMAC capability token.
        """
        return False

    def direct_sas_url(self, object_id: str, metadata: ObjectMetadata) -> str:
        """Return a full direct download URL (no fragment) for multi-read objects.

        Only called when :meth:`supports_direct_sas` is ``True``.
        """
        raise NotImplementedError
