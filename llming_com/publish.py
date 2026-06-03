"""Named app publishing: stable URLs, link lifetime, and durable device grants.

This is the identity layer that sits *in front of* the two transports
(:mod:`llming_com.access.remote` proxy hub and :mod:`llming_com.p2p`).  A host
publishes an app under an ``account/app`` slug and gets a stable, bookmarkable,
secret-free URL such as ``https://apps.example.com/acme/dashboard``.

Durability model (why a phone can reload 10 minutes — or a day — later and
reconnect without re-scanning a QR code):

- The **stable URL** carries no secret.  It only names the published app.
- Pairing (QR ``#pt=`` token or an opaque ``?k=`` share key) is redeemed *once*
  for a long-lived **device credential** that the browser stores in IndexedDB.
- Every load of the stable URL re-verifies the stored credential and performs a
  *fresh* handshake to obtain a new, ephemeral connection (a proxy session or a
  P2P viewer URL).  The credential — not the connection — is what persists.
- The credential stays valid until the published link's **lifetime** expires or
  it is revoked.  So reconnect works for as long as the link is meant to live.

The registry is transport-neutral and storage-pluggable.  The in-memory default
suits a single process and the samples; production hubs back the same shape with
durable storage (Cloudflare KV, Redis, Postgres).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

DEFAULT_PAIRING_TTL = 600.0  # opaque pairing/share token lifetime before redemption
NO_EXPIRY = 0.0


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_slug(value: str) -> str:
    """Lower-case and validate one path segment (owner handle or app segment)."""

    slug = value.strip().lower()
    if not SLUG_RE.match(slug):
        raise ValueError(
            f"invalid slug {value!r}: use 1-63 chars of a-z, 0-9, hyphen, not starting with a hyphen"
        )
    return slug


def normalize_app_path(value: str) -> str:
    """Validate a (possibly multi-segment) app path, e.g. ``com/samples/board``."""

    parts = [p for p in value.strip().lower().split("/") if p]
    if not parts:
        raise ValueError("empty app path")
    return "/".join(normalize_slug(p) for p in parts)


@dataclass
class PublishedApp:
    """One app published under ``account/app`` with a chosen lifetime."""

    account: str
    app: str
    modes: list[str] = field(default_factory=lambda: ["p2p+proxy"])
    host_id: str = ""           # proxy transport: access-hub host
    room: str = ""              # p2p transport: relay room
    relay_endpoint: str = ""    # p2p transport: relay base URL ("" = HTTP signaling on this host)
    proxy_fallback_url: str = ""
    display_name: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = NO_EXPIRY  # 0 = no expiry

    @property
    def slug(self) -> str:
        return f"{self.account}/{self.app}"

    def is_active(self, now: float | None = None) -> bool:
        if self.expires_at == NO_EXPIRY:
            return True
        return (now if now is not None else time.time()) < self.expires_at

    @property
    def mode(self) -> str:
        """Primary connectivity mode (p2p | proxy | p2p+proxy)."""

        return self.modes[0] if self.modes else "p2p+proxy"


@dataclass
class DeviceGrant:
    """A redeemed, long-lived credential tying one browser to one published app."""

    credential_hash: str
    account: str
    app: str
    expires_at: float = NO_EXPIRY
    label: str = ""

    def is_active(self, now: float | None = None) -> bool:
        if self.expires_at == NO_EXPIRY:
            return True
        return (now if now is not None else time.time()) < self.expires_at


@dataclass
class _PairingToken:
    account: str
    app: str
    expires_at: float
    label: str = ""


class PublishRegistry:
    """In-memory registry of published apps, pairing tokens, and device grants.

    Production deployments subclass or reimplement the same method surface over
    durable storage; the worker hub mirrors this in Cloudflare KV.
    """

    def __init__(self) -> None:
        self._apps: dict[str, PublishedApp] = {}
        self._pairings: dict[str, _PairingToken] = {}
        self._devices: dict[str, DeviceGrant] = {}

    # ---- publishing ----

    def publish(
        self,
        account: str,
        app: str,
        *,
        modes: list[str] | None = None,
        host_id: str = "",
        room: str = "",
        relay_endpoint: str = "",
        proxy_fallback_url: str = "",
        display_name: str = "",
        ttl_seconds: float = NO_EXPIRY,
    ) -> PublishedApp:
        account = normalize_slug(account)
        app = normalize_app_path(app)
        record = PublishedApp(
            account=account,
            app=app,
            modes=modes or ["p2p+proxy"],
            host_id=host_id,
            room=room,
            relay_endpoint=relay_endpoint,
            proxy_fallback_url=proxy_fallback_url,
            display_name=display_name or app,
            expires_at=(time.time() + ttl_seconds) if ttl_seconds and ttl_seconds > 0 else NO_EXPIRY,
        )
        self._apps[record.slug] = record
        return record

    def resolve(self, account: str, app: str, *, now: float | None = None) -> PublishedApp | None:
        record = self._apps.get(f"{account.lower()}/{app.lower()}")
        if record is None or not record.is_active(now):
            return None
        return record

    def revoke(self, account: str, app: str) -> None:
        slug = f"{account.lower()}/{app.lower()}"
        self._apps.pop(slug, None)
        # drop pairings and device grants for the app
        self._pairings = {t: p for t, p in self._pairings.items() if f"{p.account}/{p.app}" != slug}
        self._devices = {h: g for h, g in self._devices.items() if f"{g.account}/{g.app}" != slug}

    # ---- pairing tokens / opaque share links ----

    def issue_pairing(
        self,
        account: str,
        app: str,
        *,
        ttl_seconds: float = DEFAULT_PAIRING_TTL,
        label: str = "",
    ) -> str:
        """Mint a one-time pairing/share token (the ``#pt=`` / ``?k=`` value)."""

        record = self.resolve(account, app)
        if record is None:
            raise KeyError(f"no active published app for {account}/{app}")
        token = secrets.token_urlsafe(24)
        self._pairings[token] = _PairingToken(
            account=record.account,
            app=record.app,
            expires_at=time.time() + ttl_seconds,
            label=label,
        )
        return token

    def redeem_pairing(self, token: str) -> tuple[PublishedApp, str]:
        """Consume a pairing token; return the app and a fresh device credential.

        The device credential's lifetime is bounded by the published link's
        lifetime, so the browser can reconnect until the link expires.
        """

        pairing = self._pairings.pop(token, None)
        now = time.time()
        if pairing is None or now >= pairing.expires_at:
            raise KeyError("pairing token is invalid or expired")
        record = self.resolve(pairing.account, pairing.app, now=now)
        if record is None:
            raise KeyError("published app is no longer active")
        credential = secrets.token_urlsafe(32)
        self._devices[_hash(credential)] = DeviceGrant(
            credential_hash=_hash(credential),
            account=record.account,
            app=record.app,
            expires_at=record.expires_at,
            label=pairing.label,
        )
        return record, credential

    # ---- device verification (every reconnect) ----

    def verify_device(self, credential: str, *, now: float | None = None) -> DeviceGrant | None:
        grant = self._devices.get(_hash(credential))
        if grant is None or not grant.is_active(now):
            return None
        return grant

    def verify_device_hash(self, credential_hash: str, *, now: float | None = None) -> DeviceGrant | None:
        for stored_hash, grant in self._devices.items():
            if hmac.compare_digest(stored_hash, credential_hash) and grant.is_active(now):
                return grant
        return None

    def revoke_device(self, credential: str) -> None:
        self._devices.pop(_hash(credential), None)

    # ---- introspection ----

    def list_apps(self, *, now: float | None = None) -> list[PublishedApp]:
        return [a for a in self._apps.values() if a.is_active(now)]

    def device_count(self) -> int:
        now = time.time()
        return sum(1 for g in self._devices.values() if g.is_active(now))
