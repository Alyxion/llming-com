# llming P2P Viewer Assets

These files are the product-neutral browser shell for pairing and reconnecting
through a llming-compatible relay. Product repositories may brand or wrap the
pages, but the storage and handshake model should remain compatible.

## URL Model

QR codes should contain only a short-lived opaque pairing token:

```text
https://example.com/p2p/pair#pt=<opaque-pairing-token>
```

`pair.html` reads the token from the URL fragment, posts it to the configured
redeem endpoint, stores the returned paired-device credential, clears the
fragment from browser history, and redirects to a stable app URL:

```text
https://example.com/p2p/app
```

The stable URL contains no secrets. It can be reloaded, bookmarked, opened after
phone sleep, or loaded inside a native mobile WebView. It reads stored
credentials and starts a fresh handshake when requested.

## Config

Each page can embed a JSON config block:

```html
<script id="llming-p2p-config" type="application/json">
{
  "redeemUrl": "/p2p/api/pair/redeem",
  "handshakeUrl": "",
  "appUrl": "/p2p/app",
  "autoConnect": false
}
</script>
```

If `handshakeUrl` is empty, `app.html` uses the public relay mailbox contract:

```text
POST /{room}/connect
GET  /{room}/response?h={device_token_hash}
```

If `handshakeUrl` is set, the page posts `{ "pairing_id": "..." }` to that
endpoint and expects `{ "url": "..." }`.

## Redeem Response

The redeem endpoint should return either the pairing object directly or under a
`pairing` key:

```json
{
  "pairing": {
    "id": "device-1",
    "room_id": "room-abc",
    "relay_endpoint": "https://relay.example.com",
    "device_credential": "plaintext-shown-only-to-device",
    "expires_at": "2026-05-18T12:00:00Z",
    "app": {
      "name": "Example App"
    }
  }
}
```

The browser stores this in IndexedDB with a localStorage fallback. Native apps
should store the same data shape in platform secure storage.
