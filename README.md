# Brains website

The published site for [brains-ai](https://github.com/xibodev/brains-ai), served by GitHub
Pages from this branch at <https://xibodev.github.io/brains-ai/>.

## Editing

`index.html` is self-contained — markup, styles, and behaviour in one file, no build step and
no external assets. Open it directly in a browser to preview.

Keep the existing visual identity and edit content outside the generated markers normally.
Preserve the paired HTML comments named `brains:release-version`, `brains:mcp-count`, and
`brains:release-history` (each uses `:start` and `:end`). The version appears twice, the
MCP count once, and the history has exactly one region. Automation replaces their contents;
manual edits inside them will be overwritten. Keep layout styles outside those regions.

The history renderer uses `.release-entry` articles with `.release-meta` (linked version,
publication time, and optional `.release-badge`) and `.release-details` (optional title,
escaped plain-text highlights, and `.release-link`). It renders up to six recent releases
as static HTML, with no browser API request or JavaScript requirement. Release highlights
come from published GitHub release notes, not inferred history.

[GitHub Releases](https://github.com/xibodev/brains-ai/releases) is canonical for release
notes and downloads. Maintain the generator and release automation on the
[main branch](https://github.com/xibodev/brains-ai/tree/main), not this Pages branch; see
the [release conditions](https://github.com/xibodev/brains-ai/blob/main/docs/QUALITY_GATES.md#recurring-release-conditions)
and [automation workflows](https://github.com/xibodev/brains-ai/tree/main/.github/workflows).

## Content sources

The page describes the current Brains installation using these repository sources:

- Product overview and audience: `docs/product/PRODUCT_BRIEF.md`
- Setup and agent workflows: `docs/GUIDE.md`
- MCP tools: `docs/MCP.md` and `CORE_MCP_TOOLS` in `src/brains/capabilities.py`
- Service setup, wiring, and recovery: `docs/OPERATIONS.md`
- System structure: `docs/ARCHITECTURE.md`
- Contribution and security guidance: `CONTRIBUTING.md` and `SECURITY.md`
