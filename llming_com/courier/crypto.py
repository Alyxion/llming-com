"""Client-side cryptography for the exchange (§5.2, §3.6).

The producer encrypts with AES-256-GCM under a fresh random 256-bit key per
object *before* uploading, so the storage backend only ever holds ciphertext.
The key travels solely in the capability URL fragment (``#k=``) and is never
sent to the backend — this is what enables credential-free downloads while
keeping the backend blind to plaintext.

Wire layout of an encrypted blob::

    [ 12-byte nonce ][ ciphertext || 16-byte GCM tag ]

The nonce is prepended (§3.6) so the blob is self-describing; the key is the
only out-of-band secret, and it lives in the URL fragment.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .exceptions import IntegrityError

#: AES-256 → 32-byte key.
KEY_SIZE = 32
#: GCM standard nonce size.
NONCE_SIZE = 12
#: Algorithm identifier stamped into object metadata (§3.4).
ALGORITHM = "AES-256-GCM"


def generate_key() -> bytes:
    """Return a fresh cryptographically-random 256-bit data-encryption key."""
    return os.urandom(KEY_SIZE)


def b64u_encode(data: bytes) -> str:
    """URL-safe base64 without padding (suitable for a URL fragment)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """Inverse of :func:`b64u_encode`, tolerating missing padding."""
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 of *data* (the plaintext digest verified after decrypt)."""
    return hashlib.sha256(data).hexdigest()


def encrypt(plaintext: bytes, key: bytes, *, associated_data: bytes | None = None) -> bytes:
    """Encrypt *plaintext* with AES-256-GCM, returning ``nonce || ciphertext``.

    A fresh random nonce is generated per call and prepended to the output.
    """
    if len(key) != KEY_SIZE:
        raise ValueError(f"key must be {KEY_SIZE} bytes, got {len(key)}")
    nonce = os.urandom(NONCE_SIZE)
    ct = AESGCM(key).encrypt(nonce, plaintext, associated_data)
    return nonce + ct


def decrypt(blob: bytes, key: bytes, *, associated_data: bytes | None = None) -> bytes:
    """Decrypt a ``nonce || ciphertext`` blob produced by :func:`encrypt`.

    Raises :class:`IntegrityError` if the GCM auth tag does not verify (i.e.
    the ciphertext was tampered with or the wrong key was supplied).
    """
    if len(key) != KEY_SIZE:
        raise ValueError(f"key must be {KEY_SIZE} bytes, got {len(key)}")
    if len(blob) < NONCE_SIZE:
        raise IntegrityError("ciphertext shorter than the nonce")
    nonce, ct = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
    try:
        return AESGCM(key).decrypt(nonce, ct, associated_data)
    except Exception as exc:  # cryptography raises InvalidTag
        raise IntegrityError("AES-GCM authentication failed") from exc


def verify_sha256(plaintext: bytes, expected_hex: str) -> None:
    """Raise :class:`IntegrityError` if *plaintext* does not match the digest."""
    actual = sha256_hex(plaintext)
    if actual != expected_hex:
        raise IntegrityError(
            f"plaintext SHA-256 mismatch: expected {expected_hex}, got {actual}"
        )
