from __future__ import annotations

import re
from urllib.parse import urlparse

from brains.config import settings
from brains.context.docs_indexer import search_docs
from brains.context.repo_indexer import search_repo

_URL_PATTERN = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)


def _extract_urls(text: str) -> list[str]:
    return sorted(set(_URL_PATTERN.findall(text)))


def _source_label(url: str) -> str:
    host = urlparse(url).hostname or ""
    if settings.source_allowlist and host in settings.source_allowlist:
        return "allowlisted_external"
    return "untrusted_external"


def build_context_pack(prompt: str, repo_path: str = ".", limit: int = 10) -> dict:
    docs_results = search_docs(repo_path, prompt, limit=limit)
    repo_results = search_repo(repo_path, prompt)[:limit]
    external_urls = _extract_urls(prompt)

    return {
        "query": prompt,
        "context_sources": [
            {"type": "local_docs", "trust": "trusted_local", "count": len(docs_results)},
            {"type": "local_repo", "trust": "trusted_local", "count": len(repo_results)},
            {
                "type": "external_docs",
                "trust": "untrusted_external",
                "items": [
                    {"url": url, "trust": _source_label(url), "label": "untrusted input"}
                    for url in external_urls
                ],
            },
        ],
        "results": docs_results or repo_results,
    }
