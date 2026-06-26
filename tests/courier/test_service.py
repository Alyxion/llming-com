"""ExchangeService: TTL policy, capability URLs, single-use, expiry, delete."""

from __future__ import annotations

from datetime import timedelta

import pytest

from llming_com.courier.exceptions import ForbiddenError, NotFoundError, TTLExceededError
from llming_com.courier.service import ExchangeService
from llming_com.courier.storage.memory import InMemoryBackend
from llming_com.courier.tokens import mint
from llming_com.courier.url import parse_capability_url


def test_upload_download_roundtrip(service):
    resp = service.upload(b"opaque-bytes", content_type="application/pdf")
    parsed = parse_capability_url(resp.url)
    data, meta = service.download(parsed.object_id, parsed.sas_query)
    assert data == b"opaque-bytes"
    assert meta.content_type == "application/pdf"


def test_url_carries_no_key_fragment(service):
    # The service is key-blind; the producer appends #k itself.
    resp = service.upload(b"x")
    assert "#k=" not in resp.url


def test_storage_never_holds_plaintext(service, backend):
    # At-rest encryption is unconditional: even a plaintext (encrypted=false)
    # upload must be ciphertext in the backing store. The marker must not appear
    # anywhere in the stored bytes, and the bytes must differ from the payload.
    payload = b"SECRET-MARKER-payload-bytes"
    resp = service.upload(payload, encrypted=False)
    parsed = parse_capability_url(resp.url)
    stored = backend._objects[parsed.object_id].data
    assert stored != payload
    assert b"SECRET-MARKER" not in stored
    # ...yet it round-trips back to the original through get().
    data, _ = service.download(parsed.object_id, parsed.sas_query)
    assert data == payload


def test_default_ttl_applied(service, settings):
    resp = service.upload(b"x")
    expected = settings.default_ttl_seconds
    delta = (resp.expiry - service._now()).total_seconds()
    assert abs(delta - expected) < 5


def test_ttl_over_max_rejected(service, settings):
    with pytest.raises(TTLExceededError):
        service.upload(b"x", ttl_seconds=settings.max_ttl_seconds + 1)


def test_single_use_deleted_after_download(service, backend):
    resp = service.upload(b"one-shot", single_use=True)
    parsed = parse_capability_url(resp.url)
    assert resp.single_use is True
    data, _ = service.download(parsed.object_id, parsed.sas_query)
    assert data == b"one-shot"
    # Second read is gone (streamed-then-deleted).
    with pytest.raises(NotFoundError):
        service.download(parsed.object_id, parsed.sas_query)


def test_multi_read_survives_within_ttl(service):
    resp = service.upload(b"reusable", single_use=False)
    parsed = parse_capability_url(resp.url)
    for _ in range(3):
        data, _ = service.download(parsed.object_id, parsed.sas_query)
        assert data == b"reusable"


def test_download_requires_valid_token(service):
    resp = service.upload(b"x")
    parsed = parse_capability_url(resp.url)
    with pytest.raises(ForbiddenError):
        service.download(parsed.object_id, "se=1&sig=forged")


def test_expired_object_not_found(service, backend):
    resp = service.upload(b"x", ttl_seconds=1)
    parsed = parse_capability_url(resp.url)
    # Force expiry by rewriting stored metadata into the past.
    entry = backend._objects[parsed.object_id]
    entry.metadata.expiry = service._now() - timedelta(seconds=1)
    # Re-mint a token that is itself still valid, to isolate object expiry.
    token = mint(parsed.object_id, service._now() + timedelta(hours=1), service.settings.signing_key)
    with pytest.raises(NotFoundError):
        service.download(parsed.object_id, token)


def test_early_delete(service, backend):
    resp = service.upload(b"x")
    parsed = parse_capability_url(resp.url)
    service.delete_object(parsed.object_id)
    assert len(backend) == 0
    with pytest.raises(NotFoundError):
        service.download(parsed.object_id, parsed.sas_query)


class _DirectSasBackend(InMemoryBackend):
    """A backend that advertises direct-SAS, for exercising prefer_direct_sas."""

    def supports_direct_sas(self) -> bool:
        return True

    def direct_sas_url(self, object_id, metadata):
        return f"https://acct.blob.core.windows.net/exchange/{object_id}?sig=fake"


def test_prefer_direct_sas_true_uses_blob_url(settings):
    settings.prefer_direct_sas = True
    svc = ExchangeService(_DirectSasBackend(), settings)
    resp = svc.upload(b"x", single_use=False)
    assert resp.url.startswith("https://acct.blob.core.windows.net/")


def test_prefer_direct_sas_false_forces_function_mediated(settings):
    settings.prefer_direct_sas = False
    svc = ExchangeService(_DirectSasBackend(), settings)
    resp = svc.upload(b"x", single_use=False)
    # Routed through the host download endpoint, not the blob account.
    assert resp.url.startswith(settings.public_base_url + "/courier/o/")


def test_single_use_always_function_mediated_even_with_direct_sas(settings):
    settings.prefer_direct_sas = True
    svc = ExchangeService(_DirectSasBackend(), settings)
    resp = svc.upload(b"x", single_use=True)
    assert resp.url.startswith(settings.public_base_url + "/courier/o/")


def test_object_ids_are_unique_and_opaque(service):
    ids = {service.upload(b"x").object_id for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 64 for i in ids)  # 256-bit hex
