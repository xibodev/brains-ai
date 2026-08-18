"""Best-effort background pre-indexing on session start.

When a session opens, build the code graph and embed the repo in a daemon thread
so the FIRST retrieval call is instant instead of paying the cold-index latency.

Entirely best-effort and cheap-when-warm: gated by config, deduped per workspace
path, and idempotent — it does a quick DB check first and only indexes what is
missing, so repeated session starts on an already-indexed repo do ~no work. It
never raises into the caller. Indexing is LOCAL (AST parse for the graph, a local
Ollama model for embeddings) — it spends free local compute, not paid LLM tokens.
"""

from __future__ import annotations

import threading
from pathlib import Path

from brains.config import settings
from brains.control.common import normalize_path

_inflight: set[str] = set()
_lock = threading.Lock()


def _index_state(path: str) -> tuple[bool, bool]:
    """Return ``(graph_built, embeds_present)`` for the workspace at ``path``.

    A cheap DB-only check so a warm repo skips all work.
    """
    from brains.context.semantic import _resolve_repo_source
    from brains.control.sessions import register_workspace
    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import CodeGraphNode

    init_db()
    with SessionLocal() as session:
        workspace = register_workspace(path)
        graph_built = (
            session.query(CodeGraphNode.id)
            .filter(CodeGraphNode.workspace_id == workspace.id)
            .first()
            is not None
        )
        source, _root, _status = _resolve_repo_source(session, path)
        embeds_present = source is not None
    return graph_built, embeds_present


def _run(path: str) -> None:
    import contextlib

    try:
        from brains.context.code_graph import build_code_graph
        from brains.context.semantic import _count_candidate_files, embed_repo

        graph_built, embeds_present = _index_state(path)
        if settings.graph_auto_build and not graph_built:
            with contextlib.suppress(Exception):
                build_code_graph(path)
        if settings.semantic_auto_embed and settings.embed_model and not embeds_present:
            cap = settings.auto_index_max_files
            if not cap or _count_candidate_files(path, cap) <= cap:
                with contextlib.suppress(Exception):
                    embed_repo(path)
    except Exception:
        pass
    finally:
        with _lock:
            _inflight.discard(_key(path))


def _key(path: str) -> str:
    try:
        return str(Path(normalize_path(path)).expanduser())
    except (OSError, ValueError):
        return str(path)


def schedule_prewarm(workspace_path: str) -> bool:
    """Schedule a best-effort background pre-index for ``workspace_path``.

    Returns ``True`` if a worker thread was started, ``False`` if skipped
    (disabled, already in-flight for this path, or not a real directory). Never
    raises.
    """
    if not settings.prewarm_index_on_session:
        return False
    if not (settings.graph_auto_build or settings.semantic_auto_embed):
        return False
    key = _key(workspace_path)
    if not Path(key).is_dir():
        return False
    with _lock:
        if key in _inflight:
            return False
        _inflight.add(key)
    try:
        thread = threading.Thread(
            target=_run, args=(workspace_path,), name="brains-prewarm", daemon=True
        )
        thread.start()
    except Exception:
        with _lock:
            _inflight.discard(key)
        return False
    return True


__all__ = ["schedule_prewarm"]
