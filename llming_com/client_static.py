"""Client-side static assets for llming-com.

Provides the ``LlmingWebSocket`` JavaScript class and a helper
to mount it on a FastAPI/Starlette app.

Usage::

    from llming_com.client_static import mount_client_static

    prefix = mount_client_static(app)
    # JS available at: /llming-com/llming-ws.js
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"


def mount_client_static(app, path: str = "/llming-com") -> str:
    """Mount the llming-com client JavaScript files on *app*.

    Returns the mount *path* so callers can build script URLs::

        prefix = mount_client_static(app)
        # <script src="{prefix}/llming-ws.js"></script>
    """
    from starlette.staticfiles import StaticFiles

    app.mount(path, StaticFiles(directory=STATIC_DIR), name="llming-com-static")
    return path
