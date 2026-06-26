"""Crypto round-trip, tamper detection, and key/digest helpers."""

from __future__ import annotations

import pytest

from llming_com.courier.crypto import (
    KEY_SIZE,
    NONCE_SIZE,
    b64u_decode,
    b64u_encode,
    decrypt,
    encrypt,
    generate_key,
    sha256_hex,
    verify_sha256,
)
from llming_com.courier.exceptions import IntegrityError


def test_encrypt_decrypt_roundtrip():
    key = generate_key()
    plaintext = b"the bytes never touch the model context" * 100
    blob = encrypt(plaintext, key)
    # Nonce is prepended; ciphertext differs from plaintext.
    assert len(blob) >= len(plaintext) + NONCE_SIZE
    assert blob[NONCE_SIZE:] != plaintext
    assert decrypt(blob, key) == plaintext


def test_generate_key_is_256_bit_and_random():
    assert len(generate_key()) == KEY_SIZE
    assert generate_key() != generate_key()


def test_distinct_nonce_per_call():
    key = generate_key()
    a = encrypt(b"same", key)
    b = encrypt(b"same", key)
    assert a[:NONCE_SIZE] != b[:NONCE_SIZE]
    assert a != b


def test_tampered_ciphertext_raises():
    key = generate_key()
    blob = bytearray(encrypt(b"payload", key))
    blob[-1] ^= 0x01  # flip a bit in the GCM tag region
    with pytest.raises(IntegrityError):
        decrypt(bytes(blob), key)


def test_wrong_key_raises():
    blob = encrypt(b"payload", generate_key())
    with pytest.raises(IntegrityError):
        decrypt(blob, generate_key())


def test_bad_key_size_rejected():
    with pytest.raises(ValueError):
        encrypt(b"x", b"too-short")


def test_b64u_roundtrip():
    for n in range(0, 40):
        raw = bytes(range(n))
        assert b64u_decode(b64u_encode(raw)) == raw


def test_sha256_verification():
    data = b"belege"
    verify_sha256(data, sha256_hex(data))
    with pytest.raises(IntegrityError):
        verify_sha256(data, sha256_hex(b"different"))
