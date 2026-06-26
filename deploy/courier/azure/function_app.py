"""Azure Functions host (Python v2 model) — the production upload provider.

HTTP-triggered, upload-centric (§2). It validates the bearer key, size and
content-type; stores the posted (ciphertext) bytes; stamps native expiry; and
returns the assembled capability URL. The Function holds no plaintext and no
decryption key — the key lives only in the URL fragment the producer appends.

All routes are served under the ``courier`` host route prefix (configured in
``host.json``), so the function routes below resolve at ``/courier/...``:

* ``POST /courier/upload``     — upload (bearer auth)
* ``GET  /courier/o/{id}``     — single-use one-time download (multi-read uses direct SAS)
* ``DELETE /courier/o/{id}``   — early delete (bearer auth)

Deploy this folder; ``build.sh`` vendors the ``llming_com.courier`` subpackage
(with a stub top-level ``llming_com`` package so the heavy framework
``__init__`` is *not* pulled in) and ``requirements.txt`` here supplies the
third-party deps.
"""

from __future__ import annotations

import json

import azure.functions as func

from llming_com.courier.azure_host import build_service
from llming_com.courier.config import get_settings, parse_ttl
from llming_com.courier.exceptions import CourierError, PayloadTooLargeError, ValidationError
from llming_com.courier.server.auth import check_bearer

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

_settings = get_settings()
_service = build_service()


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _error(exc: CourierError) -> func.HttpResponse:
    body = json.dumps({"code": exc.code, "message": str(exc)})
    return func.HttpResponse(body, status_code=exc.status, mimetype="application/json")


@app.route(route="upload", methods=["POST"])
def upload(req: func.HttpRequest) -> func.HttpResponse:
    try:
        check_bearer(req.headers.get("Authorization"), _settings)
        body = req.get_body()
        if len(body) > _settings.max_upload_bytes:
            raise PayloadTooLargeError("body exceeds max upload size")

        params = req.params
        try:
            ttl_seconds = parse_ttl(params["ttl"]) if "ttl" in params else None
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        result = _service.upload(
            body,
            content_type=params.get("contentType")
            or req.headers.get("Content-Type", "application/octet-stream"),
            ttl_seconds=ttl_seconds,
            single_use=_bool(params.get("singleUse"), _settings.default_single_use),
            producer_id=params.get("producerId"),
            sensitivity=params.get("sensitivity", "regulated"),
            encrypted=_bool(params.get("encrypted"), True),
            sha256=params.get("sha256"),
        )
        return func.HttpResponse(
            result.model_dump_json(), status_code=201, mimetype="application/json"
        )
    except CourierError as exc:
        return _error(exc)


@app.route(route="o/{object_id}", methods=["GET"])
def download(req: func.HttpRequest) -> func.HttpResponse:
    """One-time download for single-use objects (streams then deletes, §3.5)."""
    try:
        object_id = req.route_params["object_id"]
        data, meta = _service.download(object_id, req.url.split("?", 1)[-1])
        return func.HttpResponse(data, status_code=200, mimetype=meta.content_type)
    except CourierError as exc:
        return _error(exc)


@app.route(route="o/{object_id}", methods=["DELETE"])
def delete(req: func.HttpRequest) -> func.HttpResponse:
    try:
        check_bearer(req.headers.get("Authorization"), _settings)
        _service.delete_object(req.route_params["object_id"])
        return func.HttpResponse(status_code=204)
    except CourierError as exc:
        return _error(exc)
