"""Dev-server capability token (SAS-equivalent) signing and expiry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from llming_com.courier.exceptions import ForbiddenError
from llming_com.courier.tokens import mint, verify

SECRET = "unit-test-secret"


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=2)


def test_valid_token_verifies():
    q = mint("obj-1", _future(), SECRET)
    verify("obj-1", q, SECRET)  # no raise


def test_token_bound_to_object_id():
    q = mint("obj-1", _future(), SECRET)
    with pytest.raises(ForbiddenError):
        verify("obj-2", q, SECRET)


def test_tampered_signature_rejected():
    q = mint("obj-1", _future(), SECRET) + "00"
    with pytest.raises(ForbiddenError):
        verify("obj-1", q, SECRET)


def test_wrong_secret_rejected():
    q = mint("obj-1", _future(), SECRET)
    with pytest.raises(ForbiddenError):
        verify("obj-1", q, "other-secret")


def test_expired_token_rejected():
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    q = mint("obj-1", past, SECRET)
    with pytest.raises(ForbiddenError):
        verify("obj-1", q, SECRET)


def test_malformed_query_rejected():
    with pytest.raises(ForbiddenError):
        verify("obj-1", "garbage", SECRET)
