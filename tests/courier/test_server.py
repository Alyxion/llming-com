"""FastAPI dev server — the wire contract (§3.0) end to end via TestClient."""

from __future__ import annotations

from urllib.parse import urlsplit

from llming_com.courier.crypto import b64u_encode, decrypt, encrypt, generate_key, sha256_hex
from llming_com.courier.url import parse_capability_url

from .conftest import API_KEY

AUTH = {"Authorization": f"Bearer {API_KEY}"}


def _path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "")


def test_healthz(client):
    assert client.get("/courier/healthz").json() == {"status": "ok"}


def test_upload_requires_bearer(client):
    assert client.post("/courier/upload", content=b"x").status_code == 401
    assert client.post("/courier/upload", content=b"x", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_plain_upload_download_roundtrip(client):
    r = client.post(
        "/courier/upload?ttl=2h&contentType=text/plain&encrypted=false",
        content=b"hello world",
        headers=AUTH,
    )
    assert r.status_code == 201
    body = r.json()
    assert "url" in body and "expiry" in body

    got = client.get(_path(body["url"]))
    assert got.status_code == 200
    assert got.content == b"hello world"
    assert got.headers["content-type"].startswith("text/plain")


def test_end_to_end_encrypted_via_raw_http(client):
    # Simulate a producer doing client-side AES-256-GCM before POST (§5.2).
    key = generate_key()
    plaintext = b"regulated payload" * 50
    ciphertext = encrypt(plaintext, key)

    r = client.post(
        f"/courier/upload?sha256={sha256_hex(plaintext)}",
        content=ciphertext,
        headers={**AUTH, "Content-Type": "application/octet-stream"},
    )
    url = r.json()["url"] + f"#k={b64u_encode(key)}"

    # Consumer: GET ciphertext, then decrypt with the fragment key.
    parsed = parse_capability_url(url)
    got = client.get(_path(parsed.object_url))
    assert decrypt(got.content, parsed.key) == plaintext


def test_single_use_download_is_one_shot(client):
    r = client.post("/courier/upload?singleUse=true&encrypted=false", content=b"once", headers=AUTH)
    path = _path(r.json()["url"])
    assert client.get(path).content == b"once"
    assert client.get(path).status_code == 404


def test_stat_head(client):
    r = client.post("/courier/upload?encrypted=false&contentType=application/json", content=b"{}", headers=AUTH)
    path = _path(r.json()["url"])
    head = client.head(path)
    assert head.status_code == 200
    assert head.headers["Content-Length"] == "2"
    assert head.headers["Content-Type"] == "application/json"
    assert "X-Object-Expiry" in head.headers


def test_forged_token_forbidden(client):
    r = client.post("/courier/upload?encrypted=false", content=b"x", headers=AUTH)
    object_id = r.json()["object_id"]
    assert client.get(f"/courier/o/{object_id}?se=1&sig=forged").status_code == 403


def test_delete_requires_auth_and_works(client):
    r = client.post("/courier/upload?encrypted=false", content=b"x", headers=AUTH)
    object_id = r.json()["object_id"]
    path = _path(r.json()["url"])

    assert client.delete(f"/courier/o/{object_id}").status_code == 401
    assert client.delete(f"/courier/o/{object_id}", headers=AUTH).status_code == 204
    assert client.get(path).status_code == 404


def test_ttl_over_max_rejected(client):
    r = client.post("/courier/upload?ttl=31d&encrypted=false", content=b"x", headers=AUTH)
    assert r.status_code == 400


def test_payload_too_large(client, settings):
    settings.max_upload_bytes = 8
    r = client.post("/courier/upload?encrypted=false", content=b"x" * 9, headers=AUTH)
    assert r.status_code == 413
