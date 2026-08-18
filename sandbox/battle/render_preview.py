"""Render the brains 'control' code graph to HTML/SVG in an ISOLATED temp DB.

Sets BRAINS_STATE_DIR + BRAINS_DB_URL to a throwaway temp dir BEFORE importing
brains, so this never touches the host ~/.brains install. Prints the output
paths for a Playwright screenshot.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="graphviz-preview-"))
os.environ["BRAINS_STATE_DIR"] = str(_tmp / "state")
(Path(os.environ["BRAINS_STATE_DIR"])).mkdir(parents=True, exist_ok=True)
os.environ["BRAINS_DB_URL"] = f"sqlite:///{(_tmp / 'brains.db').as_posix()}"
os.environ.pop("BRAINS_OPERATOR", None)

from brains.context.code_graph import build_code_graph  # noqa: E402
from brains.context.graph_viz import graph_export  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = sys.argv[1] if len(sys.argv) > 1 else str(_REPO_ROOT / "src" / "brains" / "control")
out = sys.argv[2] if len(sys.argv) > 2 else str(_tmp / "out")

built = build_code_graph(TARGET)
print("BUILT", built)
result = graph_export(TARGET, out)
print("SVG", result["svg_path"])
print("HTML", result["html_path"])
print("NODES", result["nodes"], "EDGES", result["edges"])
