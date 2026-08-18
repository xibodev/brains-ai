"""Regression tests for the SPA bundle gate's line-ending normalization contract.

``scripts/check_spa_bundle.py`` compares a fresh ``vite build`` byte-for-byte
against the committed bundle. That comparison is only meaningful if the build
input (``frontend/index.html``) and the committed output
(``src/brains/web/spa/**``) are byte-stable across platforms. Two mechanisms
are supposed to guarantee that:

* ``.gitattributes`` forces ``frontend/index.html`` to ``text eol=lf``, so a
  ``core.autocrlf=true`` checkout on Windows still builds from an LF source,
  matching what a Linux checkout builds from.
* ``.gitattributes`` marks the committed bundle ``-text``, so git never
  rewrites its line endings on checkout regardless of platform.

These tests prove both the static contract (the attributes are declared) and
the dynamic one (a CRLF source actually produces different bytes, so the gate
would catch it rather than silently normalising it away). The dynamic case
mutates the real ``frontend/index.html`` under a fixture that always restores
the original bytes, and reuses the already-installed ``frontend/node_modules``
so it stays fast.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_spa_bundle.py"
_SPEC = importlib.util.spec_from_file_location("check_spa_bundle", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
check_spa_bundle = importlib.util.module_from_spec(_SPEC)
sys.modules["check_spa_bundle"] = check_spa_bundle
_SPEC.loader.exec_module(check_spa_bundle)

FRONTEND = ROOT / check_spa_bundle.FRONTEND_DIRNAME
COMMITTED = ROOT / check_spa_bundle.BUNDLE_RELATIVE
SOURCE_HTML = FRONTEND / "index.html"
GITATTRIBUTES = ROOT / ".gitattributes"

pytestmark = pytest.mark.skipif(
    not (FRONTEND / "node_modules").is_dir(),
    reason="frontend/node_modules is not installed; run `npm ci` in frontend/ first",
)


def test_gitattributes_forces_lf_on_the_vite_entry_source() -> None:
    """The build input must not be left to `core.autocrlf` on checkout.

    Without this, a Windows checkout with `core.autocrlf=true` builds from a
    CRLF `frontend/index.html`, injecting CRLF-tainted bytes into the
    generated bundle even though the committed blob itself is LF.
    """
    text = GITATTRIBUTES.read_text(encoding="utf-8")
    assert "frontend/index.html text eol=lf" in text, (
        "frontend/index.html must be declared `text eol=lf` in .gitattributes so "
        "every checkout builds from the same bytes regardless of platform"
    )


def test_gitattributes_still_freezes_the_committed_bundle_bytes() -> None:
    """The generated bundle must stay untouched by git's own EOL conversion."""
    text = GITATTRIBUTES.read_text(encoding="utf-8")
    assert "src/brains/web/spa/** -text" in text, (
        "the committed bundle must stay `-text` or git checkout could rewrite "
        "its line endings independently of what vite actually built"
    )


def test_committed_source_html_has_no_crlf_bytes() -> None:
    """Guards the repo blob itself, independent of the working tree checkout."""
    blob = SOURCE_HTML.read_bytes()
    assert b"\r\n" not in blob, "frontend/index.html must be committed as LF"


def test_committed_bundle_has_no_crlf_bytes() -> None:
    """The generated index.html must be byte-stable LF, not inherited CRLF."""
    blob = (COMMITTED / "index.html").read_bytes()
    assert b"\r\n" not in blob, (
        "src/brains/web/spa/index.html must be LF; a CRLF source or a "
        "CRLF-converting checkout would leak into this generated file"
    )


def test_compare_trees_treats_crlf_and_lf_as_different_content(tmp_path: Path) -> None:
    """The gate's own comparator must not paper over line-ending drift.

    This is the guard against "weakening the byte comparison": two files that
    differ only by line ending are still different bytes and must be reported
    as `content differs`, never silently treated as equivalent.
    """
    built = tmp_path / "built"
    committed = tmp_path / "committed"
    built.mkdir()
    committed.mkdir()

    (built / "index.html").write_bytes(b"<html>\r\n<body></body>\r\n</html>\r\n")
    (committed / "index.html").write_bytes(b"<html>\n<body></body>\n</html>\n")

    differences = check_spa_bundle.compare_trees(built, committed)

    assert differences == ["content differs: index.html"]


def test_compare_trees_passes_for_byte_identical_lf_trees(tmp_path: Path) -> None:
    built = tmp_path / "built"
    committed = tmp_path / "committed"
    built.mkdir()
    committed.mkdir()

    (built / "index.html").write_bytes(b"<html>\n<body></body>\n</html>\n")
    (committed / "index.html").write_bytes(b"<html>\n<body></body>\n</html>\n")

    assert check_spa_bundle.compare_trees(built, committed) == []


@pytest.fixture
def crlf_checkout_of_source_html():
    """Simulate a checkout that lost the `text eol=lf` protection.

    Rewrites the real `frontend/index.html` in place with CRLF line endings -
    exactly what a `core.autocrlf=true` checkout produces for a file with no
    `eol` attribute - then restores the original bytes unconditionally.
    """
    original = SOURCE_HTML.read_bytes()
    assert b"\r\n" not in original, "fixture precondition: source must start LF-only"
    crlf = original.replace(b"\n", b"\r\n")
    SOURCE_HTML.write_bytes(crlf)
    try:
        yield
    finally:
        SOURCE_HTML.write_bytes(original)


def _build(out_dir: Path) -> None:
    entry = FRONTEND / check_spa_bundle.VITE_ENTRY
    code = check_spa_bundle._run(
        [
            check_spa_bundle._node(),
            str(entry),
            "build",
            "--outDir",
            str(out_dir),
            "--emptyOutDir",
        ],
        FRONTEND,
    )
    assert code == 0, "vite build failed"


def test_build_from_crlf_sourced_html_diverges_from_the_committed_bundle(
    tmp_path: Path, crlf_checkout_of_source_html: None
) -> None:
    """Prove the failure mode this fix closes, so a regression fails clearly.

    If `frontend/index.html` is ever checked out with CRLF again (attribute
    dropped, override, etc.), the resulting build must be caught as different
    from the committed bundle - never silently accepted as equivalent.
    """
    out_dir = tmp_path / "crlf-build"
    _build(out_dir)

    differences = check_spa_bundle.compare_trees(out_dir, COMMITTED)

    assert differences == ["content differs: index.html"], (
        "a CRLF-sourced build must diverge only in index.html; JS/CSS assets are "
        "content-hashed independently of the HTML source's line endings"
    )


def test_build_from_lf_sourced_html_matches_the_committed_bundle(tmp_path: Path) -> None:
    """The normalization contract's positive case: LF in, byte-identical out."""
    assert b"\r\n" not in SOURCE_HTML.read_bytes(), (
        "precondition failed: frontend/index.html is not LF on disk; "
        "run `git add --renormalize frontend/index.html`"
    )

    out_dir = tmp_path / "lf-build"
    _build(out_dir)

    differences = check_spa_bundle.compare_trees(out_dir, COMMITTED)

    assert differences == []


def test_full_gate_passes_against_the_committed_bundle() -> None:
    """End-to-end regression guard for this fix, reusing the installed toolchain."""
    exit_code = check_spa_bundle.main(["--no-install"])
    assert exit_code == 0
