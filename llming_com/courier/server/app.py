"""FastAPI application factory for the dev/upload server.

Every route is exposed under the ``/courier`` prefix so the Courier can be
mounted alongside the rest of an llming-com app without colliding:

* ``POST /courier/upload``      — upload (bearer auth), ``?ttl=`` / ``?singleUse=`` (§3.1)
* ``GET  /courier/o/{id}``      — credential-free download via capability token (§3.1)
* ``HEAD /courier/o/{id}``      — STAT: size/metadata without the body (§3.1)
* ``DELETE /courier/o/{id}``    — early delete / right-to-erasure (bearer auth) (§3.1)
* ``GET  /courier/healthz``     — liveness

Diagnostic logging (§3.7) records actor/objectId/size/result — never payload
bytes, never the key or SAS.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from ..config import Settings, get_settings, parse_ttl
from ..exceptions import CourierError, PayloadTooLargeError, ValidationError
from ..models import ErrorResponse
from ..service import ROUTE_PREFIX, ExchangeService
from ..storage.base import StorageBackend
from ..storage.memory import InMemoryBackend
from .auth import check_bearer

logger = logging.getLogger("llming_com.courier.server")


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_router(service: ExchangeService, settings: Settings) -> APIRouter:
    """Build the Courier API router (all routes under ``/courier``).

    Exposed separately from :func:`create_app` so the Courier can be included
    into an existing llming-com FastAPI app via ``app.include_router(...)``
    without taking over the whole application.
    """
    router = APIRouter(prefix=f"/{ROUTE_PREFIX}")

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.post("/upload")
    async def upload(request: Request) -> Response:
        check_bearer(request.headers.get("authorization"), settings)

        body = await request.body()
        if len(body) > settings.max_upload_bytes:
            raise PayloadTooLargeError(
                f"body {len(body)} bytes exceeds max {settings.max_upload_bytes}"
            )

        q = request.query_params
        try:
            ttl_seconds = parse_ttl(q["ttl"]) if "ttl" in q else None
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        single_use = _parse_bool(q.get("singleUse"), settings.default_single_use)
        content_type = q.get("contentType") or request.headers.get(
            "content-type", "application/octet-stream"
        )
        encrypted = _parse_bool(q.get("encrypted"), True)

        result = service.upload(
            body,
            content_type=content_type,
            ttl_seconds=ttl_seconds,
            single_use=single_use,
            producer_id=q.get("producerId"),
            sensitivity=q.get("sensitivity", "regulated"),
            encrypted=encrypted,
            sha256=q.get("sha256"),
        )
        logger.info(
            "upload object=%s size=%d single_use=%s",
            result.object_id,
            len(body),
            result.single_use,
        )
        # 201 Created; never log the URL (capability secret, §5.3).
        return JSONResponse(status_code=201, content=result.model_dump(mode="json"))

    @router.get("/o/{object_id}")
    async def download(object_id: str, request: Request) -> Response:
        data, meta = service.download(object_id, request.url.query)
        logger.info("download object=%s size=%d result=ok", object_id, len(data))
        return Response(content=data, media_type=meta.content_type)

    @router.head("/o/{object_id}")
    async def stat(object_id: str, request: Request) -> Response:
        s = service.stat(object_id, request.url.query)
        headers = {
            "Content-Length": str(s.size),
            "Content-Type": s.content_type,
            "X-Object-Expiry": s.expiry.isoformat(),
            "X-Single-Use": str(s.single_use).lower(),
        }
        return Response(status_code=200, headers=headers)

    @router.delete("/o/{object_id}")
    async def delete(object_id: str, request: Request) -> Response:
        check_bearer(request.headers.get("authorization"), settings)
        service.delete_object(object_id)
        logger.info("delete object=%s result=ok", object_id)
        return Response(status_code=204)

    return router


def create_app(
    *,
    settings: Settings | None = None,
    backend: StorageBackend | None = None,
    service: ExchangeService | None = None,
) -> FastAPI:
    """Build a standalone FastAPI app serving the Courier under ``/courier``.

    Injectable settings/backend/service for tests. For embedding into an
    existing app, use :func:`build_router` instead.
    """
    settings = settings or get_settings()
    if service is None:
        # Not ``backend or InMemoryBackend()``: an empty InMemoryBackend is falsy
        # (``__len__`` == 0), which would silently discard an injected backend.
        if backend is None:
            backend = InMemoryBackend()
        service = ExchangeService(backend, settings)

    if not settings.api_keys:
        logger.warning("COURIER_API_KEYS is empty — upload auth is DISABLED (dev mode).")

    app = FastAPI(title="MCP Courier", version="0.1.0")

    @app.exception_handler(CourierError)
    async def _courier_error(_: Request, exc: CourierError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=ErrorResponse(code=exc.code, message=str(exc)).model_dump(),
        )

    app.include_router(build_router(service, settings))
    return app
