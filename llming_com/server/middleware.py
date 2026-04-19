"""Multi-purpose ASGI middleware for llming applications.

Provides :class:`LlmingMiddleware` — a single middleware that handles
cross-cutting concerns for any FastAPI/Starlette app:

- **Error shielding**: catches unhandled exceptions and returns a clean
  HTML page.  Internal details (DB hostnames, stack traces, credentials)
  are logged server-side and never reach the browser.

Extensible via constructor hooks for future concerns (request logging,
timing, auth, etc.).

Usage::

    from llming_com import LlmingMiddleware, error_response

    # Middleware (catches everything at the ASGI level):
    app.add_middleware(LlmingMiddleware)

    # Route-level helper:
    try:
        return await do_work(request)
    except Exception as e:
        return error_response(e, request_path=request.url.path)
"""

from __future__ import annotations

import logging
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

logger = logging.getLogger(__name__)

# ── Error page template ──────────────────────────────────────────

_ERROR_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center;
    height: 100vh; background: #f5f5f5; color: #333;
  }}
  .card {{
    text-align: center; max-width: 420px; padding: 40px;
    background: #fff; border-radius: 12px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08);
  }}
  h2 {{ font-size: 1.4em; margin-bottom: 12px; }}
  p {{ color: #666; line-height: 1.5; margin-bottom: 20px; }}
  a {{ color: #4B8FE7; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="card">
  <h2>{heading}</h2>
  <p>{message}</p>
  <a href="/">{back_label}</a>
</div>
</body>
</html>"""


def error_response(
    exc: Exception | None = None,
    *,
    request_path: str = "",
    status_code: int = 500,
    title: str = "Error",
    heading: str = "Something went wrong",
    message: str = "The service is temporarily unavailable. "
                   "Please try again in a few moments.",
    back_label: str = "Back to home",
    log: bool = True,
) -> HTMLResponse:
    """Return a clean HTML error response.

    Logs the full exception server-side but never exposes it to the client.
    """
    if log and exc is not None:
        logger.error(
            "Unhandled exception on %s: %s",
            request_path or "(unknown)", exc, exc_info=True,
        )
    return HTMLResponse(
        _ERROR_HTML.format(
            title=title, heading=heading, message=message, back_label=back_label,
        ),
        status_code=status_code,
    )


# ── Middleware ───────────────────────────────────────────────────

class LlmingMiddleware(BaseHTTPMiddleware):
    """Multi-purpose ASGI middleware for llming applications.

    Handles:
    - Unhandled exception → clean error page (never leaks internals)

    Args:
        app: The ASGI application.
        on_error: Optional ``(request, exc) -> Response | None``.
            Return a Response to override the default error page,
            or None to use the built-in one.
    """

    def __init__(self, app, on_error: Callable | None = None):
        super().__init__(app)
        self._on_error = on_error

    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            if self._on_error:
                custom = self._on_error(request, exc)
                if custom is not None:
                    return custom
            return error_response(exc, request_path=request.url.path)
