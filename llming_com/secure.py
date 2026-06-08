"""End-to-end encryption for relayed connections (host-authenticated ECDH).

The hub proxy terminates TLS at the edge and would otherwise see plaintext
application traffic.  This module lets the host and the browser derive a shared
key that the relay never learns, so the relay forwards only ciphertext — turning
it into a *blind* relay regardless of transport (proxy WebSocket, P2P data
channel, or TURN).

Design
------

- **Curve / cipher:** ECDH **P-256** → HKDF-SHA256 → **AES-256-GCM**.  Chosen
  because both the Python ``cryptography`` library *and* browser Web Crypto
  (``crypto.subtle``) support them everywhere (unlike X25519 in Web Crypto).
- **Authentication:** the host holds a long-term identity key.  The browser
  pins the host key's *fingerprint* out-of-band — it travels in the pairing
  URL **fragment** (``#hk=…``), which browsers never send to a server, so the
  relay cannot see or forge it.  The browser then verifies the host public key
  returned during the handshake against that pin.  A malicious relay that swaps
  the key is detected.
- **Session key:** the browser contributes an *ephemeral* key per session, so a
  leaked host key does not retroactively decrypt past sessions' browser side.
  (The host key is long-term; full forward secrecy would add a host ephemeral —
  a future extension.)

The browser side of this exact scheme is implemented with Web Crypto in the
viewer; this module is the host/Python counterpart plus test helpers.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets as _secrets
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_CURVE = ec.SECP256R1()
_HKDF_INFO = b"llming-com/e2e/v1 aes-256-gcm"
_HKDF_INFO_V2 = b"llming-com/e2e/v2 aes-256-gcm"
_NONCE_LEN = 12
_PAIRING_CODE_BYTES = 16  # 128-bit host-screen code


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    # Uncompressed point (0x04 || X || Y) — the format Web Crypto raw-exports.
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def fingerprint(public_b64: str) -> str:
    """Short, URL-safe fingerprint of a raw P-256 public key (for the ``#hk`` pin)."""

    return _b64u_encode(hashlib.sha256(_b64u_decode(public_b64)).digest())[:16]


class SecureChannel:
    """Symmetric AES-256-GCM sealing under one derived session key."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("session key must be 32 bytes")
        self._aead = AESGCM(key)

    def seal(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        """Return ``nonce(12) || ciphertext`` for the given plaintext."""

        nonce = os.urandom(_NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, plaintext, aad or None)

    def open(self, blob: bytes, aad: bytes = b"") -> bytes:
        """Inverse of :meth:`seal`; raises on tamper/auth failure."""

        if len(blob) < _NONCE_LEN:
            raise ValueError("ciphertext too short")
        nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
        return self._aead.decrypt(nonce, ct, aad or None)

    def seal_b64(self, plaintext: bytes, aad: bytes = b"") -> str:
        return _b64u_encode(self.seal(plaintext, aad))

    def open_b64(self, blob_b64: str, aad: bytes = b"") -> bytes:
        return self.open(_b64u_decode(blob_b64), aad)


def _derive_channel(shared_secret: bytes) -> SecureChannel:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(shared_secret)
    return SecureChannel(key)


@dataclass(frozen=True)
class HostIdentity:
    """The host's long-term P-256 identity key.

    Persist :meth:`to_pem` (private) across restarts so a published app keeps a
    stable :attr:`fingerprint`; the browser pins that fingerprint at pairing.
    """

    _private: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> HostIdentity:
        return cls(ec.generate_private_key(_CURVE))

    @classmethod
    def from_pem(cls, pem: str) -> HostIdentity:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("not an EC private key")
        return cls(key)

    def to_pem(self) -> str:
        return self._private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

    @property
    def public_b64(self) -> str:
        """Raw P-256 public key, base64url — what the browser receives and pins."""

        return _b64u_encode(_raw_public(self._private.public_key()))

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_b64)

    def derive(self, peer_public_b64: str) -> SecureChannel:
        """v1: derive a session key against a peer's ephemeral public key."""

        peer = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, _b64u_decode(peer_public_b64))
        shared = self._private.exchange(ec.ECDH(), peer)
        return _derive_channel(shared)

    def sign(self, data: bytes) -> str:
        """Sign with the long-term identity (ECDSA P-256/SHA-256) → base64url.

        Returns the signature in raw **P1363** form (``r‖s``, 64 bytes) — the
        format browser Web Crypto's ``verify`` expects — not DER.  Used to
        authenticate a per-session ephemeral public key so a relay can't
        substitute the host's key; the browser verifies against the pinned
        identity (``#hk`` fingerprint).
        """

        der = self._private.sign(data, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return _b64u_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


@dataclass(frozen=True)
class EphemeralKey:
    """A short-lived keypair (browser side in JS; here for tests/native peers)."""

    _private: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> EphemeralKey:
        return cls(ec.generate_private_key(_CURVE))

    @property
    def public_b64(self) -> str:
        return _b64u_encode(_raw_public(self._private.public_key()))

    def derive(self, host_public_b64: str) -> SecureChannel:
        """v1: derive a session key against the host's public key."""

        host = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, _b64u_decode(host_public_b64))
        shared = self._private.exchange(ec.ECDH(), host)
        return _derive_channel(shared)

    def shared_secret(self, peer_public_b64: str) -> bytes:
        """Raw ECDH shared secret (X-coordinate) with a peer's public key."""

        peer = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, _b64u_decode(peer_public_b64))
        return self._private.exchange(ec.ECDH(), peer)


# ---------------------------------------------------------------------------
# v2: forward-secret, host-screen-code-bound key agreement
#
# K = HKDF(IKM = ECDH(eph_a, eph_b), salt = SHA256(normalized code), info =
#          host_pubkey || device_eph_pub || host_eph_pub)
#
# - ephemeral×ephemeral ECDH  → forward secrecy
# - host ephemeral is signed by the pinned identity → host authentication
# - the host-screen code in the salt → an untrusted relay (which never sees the
#   code) cannot derive K or MITM, even with the full transcript.
# ---------------------------------------------------------------------------


def verify_signature(signer_public_b64: str, data: bytes, signature_b64: str) -> bool:
    """Verify a raw **P1363** (``r‖s``) ECDSA P-256/SHA-256 signature."""

    try:
        raw = _b64u_decode(signature_b64)
        if len(raw) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        key = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, _b64u_decode(signer_public_b64))
        key.verify(der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def generate_pairing_code() -> str:
    """A 128-bit host-screen code as grouped, copy-pastable base32 (no padding)."""

    raw = _secrets.token_bytes(_PAIRING_CODE_BYTES)
    b32 = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(b32[i : i + 4] for i in range(0, len(b32), 4))


def normalize_code(code: str) -> bytes:
    """Canonicalize a typed code so dashes / spaces / case don't matter."""

    return "".join(ch for ch in code.upper() if ch.isalnum()).encode("ascii")


def session_info(host_public_b64: str, device_eph_b64: str, host_eph_b64: str) -> bytes:
    """Transcript binding for HKDF ``info`` — pins identity + both ephemerals."""

    return b"|".join(p.encode("ascii") for p in (host_public_b64, device_eph_b64, host_eph_b64))


def derive_session_key(shared_secret: bytes, code: str, *, info: bytes) -> SecureChannel:
    """v2 key schedule: ECDH secret + host-screen code → AES-256-GCM channel."""

    salt = hashlib.sha256(normalize_code(code)).digest()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_HKDF_INFO_V2 + b"\x00" + info).derive(
        shared_secret
    )
    return SecureChannel(key)


# 1-byte frame tags so a single sealed binary blob can carry either a JSON
# control frame (str) or a raw WebSocket binary frame (bytes) — the two shapes
# the DataChannelProxy protocol uses.
_TAG_JSON = b"J"
_TAG_BIN = b"B"


class SecureFramer:
    """Encrypt the DataChannelProxy wire protocol over any byte transport.

    Sits between a transport ``peer`` (anything with async ``send(bytes|str)``)
    and a :class:`~llming_com.p2p.proxy.DataChannelProxy`.  Outbound frames are
    sealed; inbound sealed blobs are opened and returned as the original
    ``str``/``bytes`` frame.  ``DataChannelProxy`` itself needs no changes — pass
    a framer instance as its ``peer`` and feed raw wire bytes through
    :meth:`feed`.  The relay (proxy WS, P2P channel, or TURN) only ever sees the
    ciphertext.
    """

    def __init__(self, peer: Any, channel: SecureChannel) -> None:
        self._peer = peer
        self._channel = channel

    async def send(self, data: bytes | str) -> None:
        if isinstance(data, str):
            blob = self._channel.seal(_TAG_JSON + data.encode("utf-8"))
        else:
            blob = self._channel.seal(_TAG_BIN + bytes(data))
        await self._peer.send(blob)

    def feed(self, raw: bytes | str) -> bytes | str:
        """Open a sealed wire blob → the original ``str`` (JSON) or ``bytes`` frame."""

        plain = self._channel.open(raw if isinstance(raw, bytes) else raw.encode("latin-1"))
        tag, body = plain[:1], plain[1:]
        return body.decode("utf-8") if tag == _TAG_JSON else body
