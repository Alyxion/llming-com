"""Pydantic models for wire payloads and stored metadata (§3.4).

These define the *structures* of the exchange: the JSON returned by upload,
the metadata stamped onto each object, and the STAT view. They never carry
plaintext bytes, the decryption key, or the SAS — only references and
descriptive metadata.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

#: Sensitivity labels (§3.4 / §5.6). Classify conservatively — when unsure,
#: treat as ``regulated`` and require client-side encryption.
SensitivityLabel = str  # one of: "public", "internal", "confidential", "regulated"


class ObjectMetadata(BaseModel):
    """Descriptive metadata stamped onto a stored object (§3.4).

    Contains no PII in the blob name, no key, and no SAS. ``sha256`` is the
    *plaintext* digest, verified by the consumer after decryption.
    """

    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(description="256-bit random opaque id (hex).")
    size: int = Field(description="Stored (ciphertext) byte length.", ge=0)
    sha256: str | None = Field(
        default=None, description="Plaintext SHA-256 (hex), verified after decrypt."
    )
    content_type: str = Field(default="application/octet-stream")
    producer_id: str | None = Field(default=None, description="Opaque producer identity.")
    created: datetime
    expiry: datetime
    sensitivity: SensitivityLabel = Field(default="regulated")
    single_use: bool = Field(default=False)
    encrypted: bool = Field(
        default=True,
        description=(
            "Whether the payload is end-to-end encrypted under a client-held key "
            "(the host stays key-blind, §5.2). Independent of at-rest encryption, "
            "which the storage backend always applies regardless of this flag."
        ),
    )
    algorithm: str | None = Field(
        default="AES-256-GCM", description="Crypto params (alg); nonce is prepended."
    )


class UploadResponse(BaseModel):
    """JSON returned by the upload endpoint (§3.0)."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="The capability (download) URL; treat as a secret.")
    expiry: datetime = Field(description="When the URL/object dies.")
    object_id: str
    single_use: bool = False


class StatResponse(BaseModel):
    """Result of a STAT/HEAD request (§3.1)."""

    model_config = ConfigDict(extra="forbid")

    object_id: str
    size: int
    content_type: str
    expiry: datetime
    single_use: bool


class ErrorResponse(BaseModel):
    """Uniform error envelope."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
