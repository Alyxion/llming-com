"""MCP Courier — a payload-agnostic inter-MCP exchange.

A flexible side channel that lets any MCP server hand any payload (PDFs,
images, datasets, archives, JSON — any bytes) to any other MCP server or
host-side consumer, *without* the bytes ever passing through the model's
context window. The model moves a short capability URL; the storage backend
moves the bytes.

The public surface is intentionally small:

* :func:`encrypt` / :func:`decrypt` — AES-256-GCM client-side crypto.
* :func:`build_capability_url` / :func:`parse_capability_url` — the reference
  (download URL) format, with the decryption key kept in the URL fragment.
* :class:`CourierClient` — optional convenience sugar over the plain-HTTP
  wire contract (``POST`` to upload, ``GET`` to download).
* :class:`ExchangeService` — backend-agnostic upload/download/stat/delete
  logic shared by the FastAPI dev server and the Azure Function host.

Nothing here depends on email, Microsoft Graph, or any single workflow.
"""

from __future__ import annotations

from .crypto import decrypt, encrypt, generate_key, sha256_hex
from .exceptions import (
    CourierError,
    IntegrityError,
    NotFoundError,
    PayloadTooLargeError,
    TTLExceededError,
    UnauthorizedError,
)
from .models import ObjectMetadata, StatResponse, UploadResponse
from .url import CapabilityURL, build_capability_url, parse_capability_url

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # crypto
    "encrypt",
    "decrypt",
    "generate_key",
    "sha256_hex",
    # url
    "CapabilityURL",
    "build_capability_url",
    "parse_capability_url",
    # models
    "ObjectMetadata",
    "StatResponse",
    "UploadResponse",
    # errors
    "CourierError",
    "IntegrityError",
    "NotFoundError",
    "PayloadTooLargeError",
    "TTLExceededError",
    "UnauthorizedError",
]


def __getattr__(name: str):  # pragma: no cover - thin lazy import shim
    # ``CourierClient`` and ``ExchangeService`` pull in heavier deps, so they
    # are imported lazily to keep ``import llming_com.courier`` cheap.
    if name == "CourierClient":
        from .client import CourierClient

        return CourierClient
    if name == "ExchangeService":
        from .service import ExchangeService

        return ExchangeService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
