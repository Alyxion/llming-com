"""Azure Blob Storage backend — the production path (optional ``azure`` extra).

Stores bytes in a private container, stamps per-blob expiry (native TTL, §3.5)
and index metadata (§3.4), and mints per-object read-only SAS for direct
multi-read downloads (§3.3) so bulk bytes stay off Function compute.

**Encryption at rest is unconditional.** Azure Storage encrypts every blob at
rest with always-on, platform-managed AES-256 (Storage Service Encryption) that
cannot be disabled, so persisted bytes are never plaintext on disk — the same
invariant the in-memory backend enforces with a per-instance key. SSE is
transparent on read (including the direct-SAS path), so it adds nothing to the
download flow. End-to-end encryption (the producer's ``#k=`` key the host never
sees, §5.2) is an *additional*, optional layer on top of this baseline.

Every infrastructure value (account URL, container, credentials) is injected
at construction from configuration/environment — nothing identifying is baked
into this module. Importing it requires the ``azure-storage-blob`` package.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from wsgiref.handlers import format_date_time

from ..exceptions import NotFoundError
from ..models import ObjectMetadata
from .base import StorageBackend

logger = logging.getLogger("llming_com.courier.storage.azure_blob")

try:  # pragma: no cover - exercised only where the azure extra is installed
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob import (
        BlobSasPermissions,
        BlobServiceClient,
        generate_blob_sas,
    )

    _AZURE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _AZURE_AVAILABLE = False


def _meta_to_blob(metadata: ObjectMetadata) -> dict[str, str]:
    """Flatten metadata to the string-only dict Azure blob metadata requires."""
    out: dict[str, str] = {
        "object_id": metadata.object_id,
        "created": metadata.created.isoformat(),
        "expiry": metadata.expiry.isoformat(),
        "content_type": metadata.content_type,
        "sensitivity": metadata.sensitivity,
        "single_use": str(metadata.single_use).lower(),
        "encrypted": str(metadata.encrypted).lower(),
    }
    if metadata.sha256:
        out["sha256"] = metadata.sha256
    if metadata.producer_id:
        out["producer_id"] = metadata.producer_id
    if metadata.algorithm:
        out["algorithm"] = metadata.algorithm
    return out


def _blob_to_meta(object_id: str, size: int, raw: dict[str, str]) -> ObjectMetadata:
    return ObjectMetadata(
        object_id=object_id,
        size=size,
        sha256=raw.get("sha256"),
        content_type=raw.get("content_type", "application/octet-stream"),
        producer_id=raw.get("producer_id"),
        created=datetime.fromisoformat(raw["created"]),
        expiry=datetime.fromisoformat(raw["expiry"]),
        sensitivity=raw.get("sensitivity", "regulated"),
        single_use=raw.get("single_use", "false") == "true",
        encrypted=raw.get("encrypted", "true") == "true",
        algorithm=raw.get("algorithm"),
    )


class AzureBlobBackend(StorageBackend):
    """Blob-backed storage with native expiry and direct-SAS downloads."""

    def __init__(
        self,
        *,
        account_url: str,
        container: str,
        credential: object,
        account_name: str | None = None,
        user_delegation_key: object | None = None,
    ) -> None:
        """Construct the backend.

        Args:
            account_url: e.g. ``https://<acct>.blob.core.windows.net`` (injected).
            container: private container name (injected).
            credential: a managed identity / token credential or account key.
            account_name: required to mint SAS (derived from ``account_url`` if omitted).
            user_delegation_key: optional pre-fetched key for identity-based SAS.
        """
        if not _AZURE_AVAILABLE:  # pragma: no cover
            raise RuntimeError(
                "azure-storage-blob is not installed; install the 'azure' extra"
            )
        self._service = BlobServiceClient(account_url, credential=credential)
        self._container = container
        self._account_url = account_url.rstrip("/")
        self._account_name = account_name or self._account_url.split("//", 1)[-1].split(".", 1)[0]
        self._credential = credential
        self._user_delegation_key = user_delegation_key

    def _blob(self, object_id: str):
        return self._service.get_blob_client(self._container, object_id)

    def put(self, object_id: str, data: bytes, metadata: ObjectMetadata) -> None:
        blob = self._blob(object_id)
        blob.upload_blob(
            data,
            overwrite=True,
            metadata=_meta_to_blob(metadata),
        )
        self._set_expiry(blob, metadata.expiry)

    @staticmethod
    def _set_expiry(blob, expiry: datetime) -> None:
        """Stamp native per-blob expiry (precise TTL, §3.5) on an HNS account.

        ``BlobClient`` exposes no public set-expiry method, so we drive the
        generated ``set_expiry`` operation (Blob "Set Expiry" REST API,
        ``comp=expiry``). Absolute mode needs an RFC 1123 (HTTP-date) string.
        Failures are logged, not swallowed silently — the 30-day lifecycle rule
        is only a backstop, so a broken precise expiry must be visible.
        """
        http_date = format_date_time(expiry.timestamp())
        try:
            blob._client.blob.set_expiry(expiry_options="Absolute", expires_on=http_date)
        except Exception:  # pragma: no cover - depends on live account features
            logger.warning(
                "failed to set per-blob expiry (falling back to lifecycle backstop)",
                exc_info=True,
            )

    def get(self, object_id: str) -> tuple[bytes, ObjectMetadata]:
        blob = self._blob(object_id)
        try:
            stream = blob.download_blob()
            data = stream.readall()
            props = blob.get_blob_properties()
        except ResourceNotFoundError as exc:
            raise NotFoundError(f"no object {object_id}") from exc
        meta = _blob_to_meta(object_id, len(data), dict(props.metadata or {}))
        return data, meta

    def head(self, object_id: str) -> ObjectMetadata:
        blob = self._blob(object_id)
        try:
            props = blob.get_blob_properties()
        except ResourceNotFoundError as exc:
            raise NotFoundError(f"no object {object_id}") from exc
        return _blob_to_meta(object_id, props.size, dict(props.metadata or {}))

    def delete(self, object_id: str) -> None:
        try:
            self._blob(object_id).delete_blob()
        except ResourceNotFoundError:
            pass

    # --- Direct presigned downloads --------------------------------------

    def supports_direct_sas(self) -> bool:
        return True

    def direct_sas_url(self, object_id: str, metadata: ObjectMetadata) -> str:
        now = datetime.now(timezone.utc)
        sas = generate_blob_sas(
            account_name=self._account_name,
            container_name=self._container,
            blob_name=object_id,
            permission=BlobSasPermissions(read=True),
            start=now,
            expiry=metadata.expiry,  # SAS expiry pinned to object TTL (§3.2)
            user_delegation_key=self._user_delegation_key,
            account_key=None if self._user_delegation_key else self._credential,
        )
        return f"{self._account_url}/{self._container}/{object_id}?{sas}"
