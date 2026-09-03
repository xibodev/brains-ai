"""Bounded, read-only source lookup that never needs an index or embedding model."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, TypedDict

LookupStatus = Literal["ok", "empty", "limited", "unavailable"]

MAX_RESULTS = 50
MAX_FILES = 2_000
MAX_DIRECTORIES = 500
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
CONTEXT_LINES = 1

_IGNORED_DIRS = {
    ".git",
    ".brains",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
_TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
_TEXT_NAMES = {"dockerfile", "makefile", "readme", "license"}
_SYMBOL_PATTERNS = (
    re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|fn|func|interface|trait|enum|struct|type)\s+([A-Za-z_$][\w$]*)"
    ),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?="),
    re.compile(
        r"^\s*(?:public|private|protected|internal|static|final|abstract|sealed|async|\s)+\s*[\w<>,?\[\]]+\s+([A-Za-z_$][\w$]*)\s*\("
    ),
)


class LookupResult(TypedDict):
    path: str
    line: int
    end_line: int
    snippet: str
    symbol: str | None
    match: Literal["symbol", "text"]


class LookupEnvelope(TypedDict):
    status: LookupStatus
    reason: str
    query: str
    results: list[LookupResult]
    scanned_files: int
    truncated: bool
    incomplete_reasons: list[str]


def _envelope(
    status: LookupStatus,
    reason: str,
    query: str,
    *,
    results: list[LookupResult] | None = None,
    scanned_files: int = 0,
    truncated: bool = False,
    incomplete_reasons: list[str] | None = None,
) -> LookupEnvelope:
    return {
        "status": status,
        "reason": reason,
        "query": query,
        "results": results or [],
        "scanned_files": scanned_files,
        "truncated": truncated,
        "incomplete_reasons": incomplete_reasons or [],
    }


def _symbol(line: str) -> str | None:
    for pattern in _SYMBOL_PATTERNS:
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def _eligible(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_SUFFIXES or path.name.lower() in _TEXT_NAMES


def lookup_workspace(root: str | os.PathLike[str], query: str, limit: int = 10) -> LookupEnvelope:
    """Search source text without writes, registration, indexing, or model calls.

    Results are deterministic and contain only root-relative paths. Symlinks are
    skipped so lookup cannot escape the selected workspace. OS error details are
    intentionally not returned.
    """
    needle = query.strip()
    if not needle:
        return _envelope("unavailable", "query_required", "")
    bounded_limit = min(MAX_RESULTS, max(1, int(limit)))
    candidate = Path(root).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return _envelope("unavailable", "root_missing", needle)
    except (OSError, RuntimeError):
        return _envelope("unavailable", "root_unreadable", needle)
    if not resolved.is_dir():
        return _envelope("unavailable", "root_not_directory", needle)
    try:
        with os.scandir(resolved) as entries:
            next(iter(entries), None)
    except OSError:
        return _envelope("unavailable", "root_unreadable", needle)

    lowered = needle.casefold()
    hits: list[tuple[int, str, int, LookupResult]] = []
    extra_hit = False
    scanned = 0
    scanned_bytes = 0
    visited_files = 0
    visited_directories = 0
    incomplete: set[str] = set()

    def traversal_error(_error: OSError) -> None:
        incomplete.add("traversal_unreadable")

    try:
        walker = os.walk(resolved, topdown=True, onerror=traversal_error, followlinks=False)
        for dirpath, dirnames, filenames in walker:
            visited_directories += 1
            if visited_directories > MAX_DIRECTORIES:
                incomplete.add("directory_limit_reached")
                break
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name.lower() not in _IGNORED_DIRS and not (Path(dirpath) / name).is_symlink()
            )
            for filename in sorted(filenames):
                if visited_files >= MAX_FILES:
                    incomplete.add("file_limit_reached")
                    break
                visited_files += 1
                path = Path(dirpath) / filename
                if path.is_symlink() or not _eligible(path):
                    continue
                try:
                    size = path.stat().st_size
                    if size > MAX_FILE_BYTES:
                        incomplete.add("file_size_limit_reached")
                        continue
                    if scanned_bytes + size > MAX_TOTAL_BYTES:
                        incomplete.add("byte_limit_reached")
                        break
                    with path.open("rb") as handle:
                        payload = handle.read(MAX_FILE_BYTES + 1)
                except OSError:
                    incomplete.add("file_unreadable")
                    continue
                if len(payload) > MAX_FILE_BYTES:
                    incomplete.add("file_size_limit_reached")
                    continue
                if scanned_bytes + len(payload) > MAX_TOTAL_BYTES:
                    incomplete.add("byte_limit_reached")
                    break
                scanned += 1
                scanned_bytes += len(payload)
                if b"\0" in payload[:2048]:
                    incomplete.add("file_not_text")
                    continue
                try:
                    text = payload.decode("utf-8")
                except UnicodeDecodeError:
                    incomplete.add("file_not_text")
                    continue
                lines = text.splitlines()
                rel = path.relative_to(resolved).as_posix()
                for index, line_text in enumerate(lines):
                    symbol = _symbol(line_text)
                    text_match = lowered in line_text.casefold()
                    symbol_match = symbol is not None and lowered in symbol.casefold()
                    if not text_match and not symbol_match:
                        continue
                    start = max(0, index - CONTEXT_LINES)
                    end = min(len(lines), index + CONTEXT_LINES + 1)
                    snippet = "\n".join(
                        f"{line_no + 1}: {lines[line_no]}" for line_no in range(start, end)
                    )
                    exact = bool(symbol and symbol.casefold() == lowered)
                    result: LookupResult = {
                        "path": rel,
                        "line": index + 1,
                        "end_line": end,
                        "snippet": snippet,
                        "symbol": symbol,
                        "match": "symbol" if symbol_match else "text",
                    }
                    hits.append(
                        (0 if exact else 1 if symbol_match else 2, rel.casefold(), index, result)
                    )
                    if len(hits) > bounded_limit:
                        hits.sort(key=lambda row: (row[0], row[1], row[2]))
                        hits.pop()
                        extra_hit = True
            if {"directory_limit_reached", "file_limit_reached", "byte_limit_reached"} & incomplete:
                break
    except OSError:
        incomplete.add("traversal_unreadable")

    hits.sort(key=lambda row: (row[0], row[1], row[2]))
    results = [row[3] for row in hits]
    if extra_hit:
        incomplete.add("result_limit_reached")
    reasons = sorted(incomplete)
    truncated = bool(reasons)
    if reasons:
        return _envelope(
            "limited",
            reasons[0] if len(reasons) == 1 else "scan_incomplete",
            needle,
            results=results,
            scanned_files=scanned,
            truncated=True,
            incomplete_reasons=reasons,
        )
    if not results:
        return _envelope("empty", "no_matches", needle, scanned_files=scanned, truncated=truncated)
    return _envelope(
        "ok", "matches_found", needle, results=results, scanned_files=scanned, truncated=truncated
    )


__all__ = ["LookupEnvelope", "LookupResult", "lookup_workspace"]
