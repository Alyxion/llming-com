"""FastAPI dev server — a local, cloud-free emulation of the upload Function.

Used for local development and the test suite. It speaks the exact same wire
contract (§3.0) as the production Azure Function: ``POST`` to upload with a
bearer header, ``GET`` the capability URL to download.
"""

from __future__ import annotations

__all__ = ["create_app"]


def __getattr__(name: str):
    # Lazy so importing sibling modules (e.g. ``server.auth`` on the Azure
    # Function host) does not pull in FastAPI, which is a dev/server-only dep.
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
