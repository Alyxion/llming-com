"""Backend-agnostic exchange logic shared by every host (§2, §6).

``ExchangeService`` is the single place that:

* validates TTL against the configured maximum,
* generates the opaque object id,
* stamps metadata and stores the (opaque, usually ciphertext) bytes,
* assembles the credential-free capability URL — **without** the decryption
  key, which the producer appends to the fragment afterwards so the host
  never holds it,
* serves and (for single-use) reaps downloads.

The FastAPI dev server and the Azure Function are thin adapters over this.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from . import tokens
from .config import Settings
from .exceptions import TTLExceededError
from .models import ObjectMetadata, StatResponse, UploadResponse
from .storage.base import StorageBackend
from .url import build_capability_url

#: URL prefix under which every Courier route is exposed (dev server + Function).
ROUTE_PREFIX = "courier"
#: Path segment for server-mediated (token) downloads. Kept under the same
#: ``/courier`` prefix as every other route so capability URLs resolve to the
#: host download endpoint (``{base}/courier/o/{id}``).
DOWNLOAD_ROUTE = f"{ROUTE_PREFIX}/o"


class ExchangeService:
    """Coordinates storage, metadata, TTL policy and capability-URL assembly."""

    def __init__(self, backend: StorageBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def new_object_id() -> str:
        """A 256-bit random, opaque, PII-free id (§3.2)."""
        return os.urandom(32).hex()

    def _resolve_ttl(self, ttl_seconds: int | None) -> int:
        ttl = ttl_seconds if ttl_seconds is not None else self.settings.default_ttl_seconds
        if ttl > self.settings.max_ttl_seconds:
            raise TTLExceededError(
                f"requested ttl {ttl}s exceeds max {self.settings.max_ttl_seconds}s"
            )
        if ttl <= 0:
            raise TTLExceededError("ttl must be positive")
        return ttl

    # --- Upload -----------------------------------------------------------

    def upload(
        self,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        ttl_seconds: int | None = None,
        single_use: bool | None = None,
        producer_id: str | None = None,
        sensitivity: str = "regulated",
        encrypted: bool = True,
        sha256: str | None = None,
        algorithm: str | None = "AES-256-GCM",
    ) -> UploadResponse:
        """Store *data* and return the capability URL (without the key fragment).

        The producer is responsible for client-side encryption; this method
        treats *data* as opaque. The returned URL carries no decryption key —
        the producer appends ``#k=`` afterwards (§5.2), so the host stays
        key-blind.
        """
        ttl = self._resolve_ttl(ttl_seconds)
        single = self.settings.default_single_use if single_use is None else single_use
        created = self._now()
        expiry = created + timedelta(seconds=ttl)
        object_id = self.new_object_id()

        metadata = ObjectMetadata(
            object_id=object_id,
            size=len(data),
            sha256=sha256,
            content_type=content_type,
            producer_id=producer_id,
            created=created,
            expiry=expiry,
            sensitivity=sensitivity,
            single_use=single,
            encrypted=encrypted,
            algorithm=algorithm if encrypted else None,
        )
        self.backend.put(object_id, data, metadata)

        url = self._download_url(object_id, metadata)
        return UploadResponse(
            url=url, expiry=expiry, object_id=object_id, single_use=single
        )

    def _download_url(self, object_id: str, metadata: ObjectMetadata) -> str:
        """Assemble the credential-free download URL (key fragment excluded)."""
        # Multi-read on a direct-SAS-capable backend → straight-to-blob URL so
        # bulk bytes bypass Function compute (§3.3). Single-use always routes
        # through the host so it can delete after streaming (§3.5). Operators can
        # force Function-mediated downloads via prefer_direct_sas=False (Topology
        # B / managed-identity deploys where SAS signing isn't wired).
        if (
            metadata.single_use
            or not self.settings.prefer_direct_sas
            or not self.backend.supports_direct_sas()
        ):
            token = tokens.mint(object_id, metadata.expiry, self.settings.signing_key)
            return build_capability_url(
                self.settings.public_base_url,
                DOWNLOAD_ROUTE,
                object_id,
                token,
                key=None,
            )
        return self.backend.direct_sas_url(object_id, metadata)

    # --- Download / stat / delete ----------------------------------------

    def download(self, object_id: str, query: str) -> tuple[bytes, ObjectMetadata]:
        """Validate the capability token, return bytes, and reap single-use objects."""
        tokens.verify(object_id, query, self.settings.signing_key, now=self._now())
        data, metadata = self.backend.get(object_id)
        if metadata.single_use:
            self.backend.delete(object_id)  # one-time: stream then delete (§3.5)
        return data, metadata

    def stat(self, object_id: str, query: str) -> StatResponse:
        tokens.verify(object_id, query, self.settings.signing_key, now=self._now())
        meta = self.backend.head(object_id)
        return StatResponse(
            object_id=meta.object_id,
            size=meta.size,
            content_type=meta.content_type,
            expiry=meta.expiry,
            single_use=meta.single_use,
        )

    def delete_object(self, object_id: str) -> None:
        """Early delete for right-to-erasure ahead of TTL (§3.1). API-key gated."""
        self.backend.delete(object_id)
