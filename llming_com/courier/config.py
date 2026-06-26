"""Runtime configuration (env-driven, no infrastructure secrets in source).

Every deployment-specific value — account host, container name, API keys,
signing secret — is read from the environment so that nothing identifying an
organisation or its infrastructure is ever committed to git. Defaults are
deliberately generic (``localhost``, ``example`` placeholders) and safe for
local development only.

Environment variables are prefixed ``COURIER_`` (e.g. ``COURIER_API_KEYS``).
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: 2 hours — the default object TTL (§3.5).
DEFAULT_TTL_SECONDS = 2 * 60 * 60
#: 30 days — the hard maximum TTL / lifecycle backstop (§3.5).
MAX_TTL_SECONDS = 30 * 24 * 60 * 60
#: 100 MB — default maximum upload size (§3.1).
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024

_TTL_PATTERN = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 1}


def parse_ttl(value: str | int) -> int:
    """Parse a TTL like ``2h``, ``30d``, ``900s`` or a bare integer (seconds).

    Returns the TTL in seconds. Raises ``ValueError`` on malformed input.
    """
    if isinstance(value, int):
        seconds = value
    else:
        match = _TTL_PATTERN.match(str(value))
        if not match:
            raise ValueError(f"invalid ttl: {value!r}")
        seconds = int(match.group(1)) * _TTL_UNITS[match.group(2).lower()]
    if seconds <= 0:
        raise ValueError("ttl must be positive")
    return seconds


class Settings(BaseSettings):
    """Process configuration for the dev server / Function host."""

    model_config = SettingsConfigDict(
        env_prefix="COURIER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Authentication (upload only) -------------------------------------
    # ``NoDecode`` stops pydantic-settings from JSON-decoding the env value, so a
    # plain comma-separated string (handled by the validator below) is accepted.
    api_keys: Annotated[set[str], NoDecode] = Field(
        default_factory=set,
        description="Valid upload bearer keys. Empty set disables auth (DEV ONLY).",
    )

    # --- Capability-URL assembly ------------------------------------------
    public_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL embedded in returned download links (no path).",
    )
    container: str = Field(
        default="exchange",
        description="Logical container/bucket name segment in the URL.",
    )

    # --- Signing of dev-server capability tokens --------------------------
    signing_key: str = Field(
        default="dev-insecure-signing-key-change-me",
        description="HMAC secret for dev-server SAS-equivalent tokens. Override in any real env.",
    )

    # --- Limits & policy --------------------------------------------------
    default_ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_ttl_seconds: int = MAX_TTL_SECONDS
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    default_single_use: bool = Field(
        default=False,
        description="singleUse when the uploader does not specify (recommended: false).",
    )
    prefer_direct_sas: bool = Field(
        default=True,
        description=(
            "When True, multi-read downloads use a direct-to-blob SAS URL if the "
            "backend supports it (Topology A). Set False to force Function-mediated "
            "downloads (Topology B / managed-identity deploys with no SAS signing)."
        ),
    )

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_keys(cls, v: object) -> object:
        # Allow a comma-separated env string in addition to JSON/list forms.
        if isinstance(v, str):
            return {k.strip() for k in v.split(",") if k.strip()}
        return v


def get_settings() -> Settings:
    """Construct :class:`Settings` from the current environment."""
    return Settings()
