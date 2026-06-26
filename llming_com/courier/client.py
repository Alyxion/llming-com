"""Optional client library — pure sugar over the plain-HTTP contract (§3.0, §6).

The wire protocol is two plain HTTP calls; this class just wraps
``encrypt → POST → append #k`` and ``parse → GET → decrypt → verify`` for
convenience. It uses only the standard library (``urllib``) so importing it
adds no runtime dependency. ``curl``/``requests``/``fetch`` remain equally
valid clients.

Example::

    client = CourierClient(function_url, api_key=key)
    url = client.upload(pdf_bytes, content_type="application/pdf")  # encrypts
    # ... hand the short `url` to a consumer MCP ...
    data = client.download(url)  # GET + decrypt + integrity check
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .crypto import b64u_encode, decrypt, encrypt, generate_key, sha256_hex, verify_sha256
from .exceptions import CourierError, NotFoundError, UnauthorizedError
from .url import KEY_FRAGMENT_PARAM, parse_capability_url


class CourierClient:
    """Convenience wrapper around the upload Function / dev server.

    ``function_url`` is the deployment **host root** (e.g.
    ``https://courier.example`` or ``http://localhost:8000``); the client
    targets the ``/courier`` route prefix itself. Download URLs are absolute
    (taken from the upload response), so they already carry the prefix.
    """

    def __init__(
        self,
        function_url: str,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.function_url = function_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # --- Upload -----------------------------------------------------------

    def upload(
        self,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        ttl: str | int | None = None,
        single_use: bool | None = None,
        producer_id: str | None = None,
        sensitivity: str = "regulated",
        encrypt_payload: bool = True,
    ) -> str:
        """Upload *data* and return the full capability URL (with ``#k=`` if encrypted).

        When ``encrypt_payload`` is true (the default, mandatory for regulated
        payloads, §5.2) the bytes are AES-256-GCM encrypted under a fresh key
        before upload; the key is appended to the returned URL fragment and is
        never sent to the server.
        """
        params: dict[str, str] = {
            "contentType": content_type,
            "sensitivity": sensitivity,
            "encrypted": "true" if encrypt_payload else "false",
        }
        if ttl is not None:
            params["ttl"] = str(ttl)
        if single_use is not None:
            params["singleUse"] = "true" if single_use else "false"
        if producer_id is not None:
            params["producerId"] = producer_id

        key: bytes | None = None
        if encrypt_payload:
            key = generate_key()
            params["sha256"] = sha256_hex(data)
            body = encrypt(data, key)
        else:
            body = data

        result = self._post(
            f"{self.function_url}/courier/upload?{urlencode(params)}", body, content_type
        )
        url = result["url"]
        if key is not None:
            sep = "&" if "#" in url else "#"
            url += f"{sep}{KEY_FRAGMENT_PARAM}={b64u_encode(key)}"
        return url

    # --- Download ---------------------------------------------------------

    def download(self, capability_url: str, *, expected_sha256: str | None = None) -> bytes:
        """Fetch and (if a key is present in the fragment) decrypt the payload.

        The GCM auth tag guarantees integrity on decrypt; pass
        ``expected_sha256`` to additionally verify the plaintext digest (§3.6).
        """
        parsed = parse_capability_url(capability_url)
        ciphertext = self._get(parsed.object_url)
        if parsed.key is None:
            data = ciphertext
        else:
            data = decrypt(ciphertext, parsed.key)
        if expected_sha256 is not None:
            verify_sha256(data, expected_sha256)
        return data

    # --- HTTP plumbing ----------------------------------------------------

    def _post(self, url: str, body: bytes, content_type: str) -> dict:
        headers = {"Content-Type": content_type}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._raise(exc)

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            self._raise(exc)

    @staticmethod
    def _raise(exc: urllib.error.HTTPError) -> None:
        try:
            detail = exc.read().decode("utf-8", "replace")
        finally:
            # Close deterministically: the HTTPError *is* the response, and the
            # enclosing ``with`` does not close it on the error path.
            exc.close()
        if exc.code == 401:
            raise UnauthorizedError(detail) from exc
        if exc.code == 404:
            raise NotFoundError(detail) from exc
        err = CourierError(f"HTTP {exc.code}: {detail}")
        err.status = exc.code
        raise err from exc
