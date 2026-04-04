"""Thread-safe in-memory data store for session-scoped runtime data.

Provides ``SessionDataStore`` — a namespaced key-value store for binary
blobs, images, and transient data that lives for the session duration.
"""

from __future__ import annotations

import threading
from typing import Any, Optional


class SessionDataStore:
    """Thread-safe in-memory store for session-scoped runtime data.

    Uses namespaces to separate different data types (e.g. "assets",
    "pasted_images", "pending_loads").

    All methods are class-level (shared across the process). Use namespaces
    to scope data to sessions or features.

    Security note: Namespaces should be constructed server-side (e.g.
    ``f"session:{session_id}:uploads"``) and never derived from user input
    to prevent namespace traversal attacks.
    """

    _lock = threading.Lock()
    _stores: dict[str, dict[str, Any]] = {}

    @classmethod
    def put(cls, namespace: str, key: str, value: Any) -> None:
        """Store a value under namespace/key."""
        with cls._lock:
            if namespace not in cls._stores:
                cls._stores[namespace] = {}
            cls._stores[namespace][key] = value

    @classmethod
    def get(cls, namespace: str, key: str) -> Optional[Any]:
        """Get a value by namespace/key. Returns None if not found."""
        with cls._lock:
            store = cls._stores.get(namespace)
            if store is None:
                return None
            return store.get(key)

    @classmethod
    def pop(cls, namespace: str, key: str) -> Optional[Any]:
        """Get and remove a value by namespace/key."""
        with cls._lock:
            store = cls._stores.get(namespace)
            if store is None:
                return None
            return store.pop(key, None)

    @classmethod
    def list_keys(cls, namespace: str) -> list[str]:
        """List all keys in a namespace."""
        with cls._lock:
            store = cls._stores.get(namespace)
            return list(store.keys()) if store else []

    @classmethod
    def clear_namespace(cls, namespace: str) -> int:
        """Clear all entries in a namespace. Returns count of removed entries."""
        with cls._lock:
            store = cls._stores.pop(namespace, None)
            return len(store) if store else 0

    @classmethod
    def clear_all(cls) -> None:
        """Clear all namespaces (for testing)."""
        with cls._lock:
            cls._stores.clear()
