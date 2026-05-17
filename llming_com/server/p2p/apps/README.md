# P2P Apps Hosts

Apps hosts are the browser-facing side of the shared P2P flow. They own login,
cookies, IndexedDB/local storage, pairing-token redemption, recent app lists,
and the "continue session" UI.

Relay hosts are intentionally separate. A relay host only exposes the room,
mailbox, SDP bridge, and WebSocket protocol.

```text
apps.example.com        browser-facing apps launcher
relay.example.com       relay protocol
test-apps.example.com   isolated browser-facing test launcher
test-relay.example.com  disabled-by-default test relay
```
