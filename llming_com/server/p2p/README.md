# P2P Server Assets

This directory contains deployable server-side pieces for the shared llming P2P
and proxy transport. It is intentionally organized by protocol role first, then
by deployment backend.

```text
p2p/
  relay/
    cloudflare/      Cloudflare Worker + Durable Object relay backend
```

Product repositories should depend on this layout instead of keeping their own
relay implementations at top level. Cloudflare is one supported server backend;
other backends can be added beside it, for example `fastapi/`, `node/`, or
`worker-compatible/`, as long as they implement the same public relay contract.
