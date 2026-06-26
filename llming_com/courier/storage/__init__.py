"""Storage backends for the exchange.

The core service is backend-agnostic: it talks to the :class:`StorageBackend`
interface. :class:`InMemoryBackend` powers local tests and the dev server;
:class:`AzureBlobBackend` (optional ``azure`` extra) is the production path.
"""

from __future__ import annotations

from .base import StorageBackend
from .memory import InMemoryBackend

__all__ = ["StorageBackend", "InMemoryBackend"]
