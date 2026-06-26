"""The reference (download URL) format — §3.2.

A capability URL bundles everything a consumer needs to retrieve one object::

    https://<host>/<container>/<objectId>?<read-only SAS>#k=<base64url DEK>

Two properties matter:

* The decryption key lives in the **fragment** (``#k=``). HTTP clients never
  transmit the fragment, so the storage backend stores and serves only
  ciphertext and never sees the key.
* The query carries the SAS (or, for the local dev server, an HMAC capability
  token). It locates and authorises read of exactly one object until expiry.

This module is pure string handling — no crypto, no I/O — so it is identical
whether the backend is Azure Blob or the in-memory dev store.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .crypto import b64u_decode, b64u_encode

#: Fragment parameter name carrying the data-encryption key.
KEY_FRAGMENT_PARAM = "k"


@dataclass(frozen=True, slots=True)
class CapabilityURL:
    """A parsed capability URL."""

    base_url: str
    """Scheme + host, e.g. ``https://acct.blob.core.windows.net``."""
    container: str
    object_id: str
    sas_query: str
    """Raw query string (without the leading ``?``)."""
    key: bytes | None
    """Decryption key recovered from the fragment, or ``None`` if absent."""

    @property
    def object_url(self) -> str:
        """The URL Azure/the dev server actually receives (no fragment)."""
        path = f"/{self.container}/{self.object_id}" if self.container else f"/{self.object_id}"
        scheme, netloc, *_ = urlsplit(self.base_url + "/")
        return urlunsplit((scheme, netloc, path, self.sas_query, ""))


def build_capability_url(
    base_url: str,
    container: str,
    object_id: str,
    sas_query: str,
    key: bytes | None = None,
) -> str:
    """Assemble a capability URL (§3.2).

    *sas_query* is the raw query string minted by the backend (Azure SAS, or
    the dev server's HMAC token). *key*, when supplied, is appended in the
    fragment as ``#k=<base64url>`` and therefore never leaves the client when
    the URL is later dereferenced.
    """
    base = base_url.rstrip("/")
    path = f"/{container}/{object_id}" if container else f"/{object_id}"
    query = sas_query.lstrip("?")
    url = f"{base}{path}"
    if query:
        url += f"?{query}"
    if key is not None:
        url += f"#{KEY_FRAGMENT_PARAM}={b64u_encode(key)}"
    return url


def parse_capability_url(url: str) -> CapabilityURL:
    """Parse a capability URL back into its parts, recovering the key."""
    scheme, netloc, path, query, fragment = urlsplit(url)
    base_url = urlunsplit((scheme, netloc, "", "", ""))

    segments = [s for s in path.split("/") if s]
    if not segments:
        raise ValueError("capability URL has no object path")
    object_id = segments[-1]
    container = "/".join(segments[:-1])

    key: bytes | None = None
    if fragment:
        params = parse_qs(fragment)
        raw = params.get(KEY_FRAGMENT_PARAM)
        if raw:
            key = b64u_decode(raw[0])

    return CapabilityURL(
        base_url=base_url,
        container=container,
        object_id=object_id,
        sas_query=query,
        key=key,
    )
