# llming P2P Apps: Cloudflare Backend

This Worker is a small browser-facing test/deployment shell for the shared
llming P2P flow. It is intentionally generic and is mainly useful for full
integration tests of an apps origin plus a separate relay origin.

It serves:

```text
GET  /health
GET  /
GET  /p2p/app
GET  /p2p/pair
POST /p2p/api/pair/redeem
GET  /p2p/api/apps/recent
```

The root page is the stable, bookmarkable apps launcher. It should be deployed
on an apps origin such as `apps.example.com` or `test-apps.example.com`, not on
the relay origin.

For production products, replace the test redeem/list endpoints with product
logic while keeping the same origin/storage model.
