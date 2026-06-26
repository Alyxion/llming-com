"""In-memory storage backend — local tests and the dev server.

Faithfully emulates the native-expiry semantics of Blob Storage: an object is
unreadable once past its ``expiry``, and a lazy sweep reaps expired entries on
access. Not for production (process-local, non-durable) — it exists so the
whole exchange can be exercised end-to-end with no cloud dependency.

**Encryption at rest is unconditional.** The dict only ever holds ciphertext:
every value is AES-256-GCM encrypted under a per-instance key on ``put`` and
decrypted on ``get``, so a heap/core dump never reveals a payload. This mirrors
Azure Blob's always-on server-side encryption (SSE) — the storage layer is
*never* a place where plaintext lives, regardless of whether the producer also
applied end-to-end (client-held-key) encryption on top. The at-rest key is
generated fresh per backend instance and never leaves the process; since this
store is non-durable the key needn't outlive it, so no configuration is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..crypto import decrypt, encrypt, generate_key
from ..exceptions import NotFoundError
from ..models import ObjectMetadata
from .base import StorageBackend


@dataclass(slots=True)
class _Entry:
    data: bytes
    """Ciphertext (``nonce || AES-256-GCM(payload)``) — never plaintext."""
    metadata: ObjectMetadata


class InMemoryBackend(StorageBackend):
    """A dict-backed store with TTL-on-read enforcement and at-rest encryption."""

    def __init__(self, *, at_rest_key: bytes | None = None) -> None:
        self._objects: dict[str, _Entry] = {}
        #: Process-local key for at-rest encryption. Ephemeral by default so the
        #: store never holds plaintext even in memory; injectable for tests.
        self._at_rest_key = at_rest_key or generate_key()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _live(self, object_id: str) -> _Entry:
        entry = self._objects.get(object_id)
        if entry is None:
            raise NotFoundError(f"no object {object_id}")
        if self._now() >= entry.metadata.expiry:
            # Native-expiry emulation: reap on access.
            self._objects.pop(object_id, None)
            raise NotFoundError(f"object {object_id} expired")
        return entry

    def put(self, object_id: str, data: bytes, metadata: ObjectMetadata) -> None:
        # Encrypt at rest: the dict holds ciphertext, never the supplied bytes.
        sealed = encrypt(data, self._at_rest_key)
        self._objects[object_id] = _Entry(data=sealed, metadata=metadata)

    def get(self, object_id: str) -> tuple[bytes, ObjectMetadata]:
        entry = self._live(object_id)
        # Reverse the at-rest seal before handing bytes back to the service.
        return decrypt(entry.data, self._at_rest_key), entry.metadata

    def head(self, object_id: str) -> ObjectMetadata:
        return self._live(object_id).metadata

    def delete(self, object_id: str) -> None:
        self._objects.pop(object_id, None)

    # Introspection helper for tests / metrics — never exposed over the wire.
    def __len__(self) -> int:
        return len(self._objects)
