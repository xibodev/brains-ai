# Brains website

The published site for [brains-ai](https://github.com/xibodev/brains-ai), served by GitHub
Pages from this branch at <https://xibodev.github.io/brains-ai/>.

This branch is intentionally orphaned: it shares no history with `main` or `staging`, so the
repository's documentation, traceability, and core-surface gates never scan it.

## Editing

`index.html` is self-contained — markup, styles, and behaviour in one file, no build step and
no external assets. Open it directly in a browser to preview.

## Content rule

The site may only describe capabilities the supported installation actually exposes. When the
product surface changes, update this page from the source of truth rather than from memory:

- supported MCP tools: `CORE_MCP_TOOLS` in `src/brains/capabilities.py`
- withdrawn CLI commands and routes: `WITHDRAWN_*` in the same module
- product claims and boundaries: `docs/product/PRODUCT_BRIEF.md`
- lifecycle labels: `docs/product/FEATURE_CONTRACT.md`

Withdrawn or deferred behaviour must stay presented as withdrawn or deferred.
