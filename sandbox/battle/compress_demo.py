"""Demonstrate brains reversible context compression (CCR).

With context_compression_enabled, search_knowledge returns COMPACT entries
(body capped + a knowledge:<code> ref) instead of full bodies — so an agent's
prompt carries a small preview plus a handle. retrieve_original() losslessly
expands the handle back to the full content only when the agent actually needs
it. This is the "prompt compression" lever: pay full tokens on demand, not on
every context load.

Run twice via env to contrast:
  docker exec battle-brain-a python compress_demo.py            # compression OFF
  docker exec -e BRAINS_CONTEXT_COMPRESSION_ENABLED=1 ... python compress_demo.py  # ON
"""

from __future__ import annotations

import json

from brains.config import settings
from brains.control.knowledge import search_knowledge
from brains.control.retrieve import retrieve_original

ACME = "/work/acme-platform"
CODE = "KNOW-0003"  # the long Stripe-webhook blocker


def _len(obj) -> int:
    return len(json.dumps(obj, default=str))


def main():
    on = settings.context_compression_enabled
    print(f"=== context_compression_enabled = {on} ===")

    results = search_knowledge(workspace_path=ACME)
    entry = next((r for r in results if r["code"] == CODE), results[0])

    body = entry.get("body") or ""
    print(f"search_knowledge -> {CODE}")
    print(f"  compressed flag : {entry.get('compressed', False)}")
    print(f"  ref             : {entry.get('ref', '(none)')}")
    print(f"  body chars      : {len(body)}")
    print(f"  entry JSON bytes: {_len(entry)}")

    if on:
        full = retrieve_original(f"knowledge:{CODE}")
        full_body = full.get("content") or full.get("body") or ""
        print(f"retrieve_original('knowledge:{CODE}') -> lossless expand")
        print(f"  restored body chars : {len(full_body)}")
        print(f"  full record JSON    : {_len(full)} bytes")
        ratio = (1 - len(body) / max(1, len(full_body))) * 100
        print(
            f"  preview is {len(body)}/{len(full_body)} chars "
            f"= {ratio:.1f}% smaller than the full body, fully reversible"
        )


if __name__ == "__main__":
    main()
