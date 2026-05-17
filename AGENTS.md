# Agent Notes

## Documentation

- Use Material for MkDocs for project documentation when project docs are generated or expanded.
- Use Mermaid for technical architecture and sequence diagrams.
- Use Excalidraw and exported SVGs for polished flow and concept visualizations.
- The MkDocs site is configured in `mkdocs.yml` and reads source pages from `docs/manual/`.
- Mermaid diagrams and SVG/Excalidraw images are inspectable through the same diagram lightbox pattern used by OpenHort and yikes.

## Platform Repositories

- `openhort`: public OpenHort runtime, isolated agentic service execution, public docs, and OpenHort-specific wrappers.
- `llming-com`: shared communication primitives, P2P/proxy/relay code, generic viewers, and reusable deployment baselines.
- `www_openhort_ai`: public website and Cloudflare Workers deployed for OpenHort-hosted web/API surfaces.
- `openhort-concept`: private concept repository for business logic, business policy, commercial platform decisions, and managed-service planning.

## Shared Transport Ownership

- `llming-com` is the canonical home for shared communication and transport primitives used by OpenHort and other llming applications.
- Generic P2P, proxy, relay, pairing, reconnect, DataChannel proxy, and browser viewer code belongs here, not in OpenHort.
- Shared server-side relay deployment code for a simple self-hosted relay should live here as reusable baseline infrastructure.
- Viewer assets that understand pairing credentials, IndexedDB/cookie stored device credentials, reconnect, and handshake initiation should live here as generic assets.
- Current shared homes are `llming_com/p2p/admission.py`, `llming_com/p2p/proxy.py`, `llming_com/access/remote.py`, `llming_com/mcp/`, `llming_com/server/p2p/relay/cloudflare/`, and `llming_com/static/p2p/`.
- Keep deployment backend names below the server role. For example, use `server/p2p/relay/cloudflare/`, not a top-level `cloudflare/` category.
- Product-specific projects may wrap these primitives, but they should not fork the protocol or viewer behavior.

## Product Boundary

- OpenHort consumes this transport layer for isolated execution and orchestration of agentic services.
- OpenHort commercial/private code owns accounts, billing, tenants, production policy, and customer limits.
- Keep business-specific OpenHort policy out of `llming-com`; this repository should stay generic and reusable.
