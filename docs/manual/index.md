# llming-com

`llming-com` contains shared communication primitives for llming applications:
WebSocket routing, auth helpers, debug/MCP surfaces, remote access tunnels, and
generic P2P/proxy relay building blocks.

The repository is intentionally product-neutral. Product repositories such as
OpenHort configure and wrap these primitives, but generic transport behavior
belongs here first.

## Documentation Conventions

This documentation uses Material for MkDocs.

- Use Mermaid for technical architecture, sequence, and protocol diagrams.
- Use Excalidraw and exported SVGs for polished concept and product-flow
  visualizations.
- Mermaid diagrams and SVG/Excalidraw images open in the diagram lightbox for
  inspection and zooming.

## Key Areas

| Area | Path |
| --- | --- |
| P2P admission and DataChannel proxy helpers | `llming_com/p2p/` |
| Remote access tunnel primitives | `llming_com/access/` |
| MCP HTTP/SSE and stdio transports | `llming_com/mcp/` |
| Generic P2P viewer assets | `llming_com/static/p2p/` |
| Server-side P2P relay deployment baselines | `llming_com/server/p2p/` |

Start with the [P2P workflow](p2p/workflow.md) for the current split between
generic transport and product-specific OpenHort integration.
