"""LLM request/response dump for classifier + tier analysis.

When the ``BRAINS_DUMP_DIR`` environment variable is set to a directory
path, this middleware writes every ``/v1/chat/completions`` and
``/v1/messages`` request + response (plus the brains routing decision)
as one JSONL line to ``<dump-dir>/<endpoint-tag>-<YYYY-MM-DD>.jsonl``.

When ``BRAINS_DUMP_DIR`` is unset (the default), :func:`install` is a
no-op — no middleware is registered, no module attributes are touched,
zero overhead on every request.

Investigation tool only. Not intended for production traffic — the
dumps contain raw prompts/responses verbatim and grow without bound.

The routing decision (classified task type, picked tier, provider, and
model) is read from ``request.state.brains_route``, which the OpenAI
and Anthropic endpoint handlers populate immediately after calling the
router. If absent, the dump still captures raw I/O with a ``null``
``brains_route`` field.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger(__name__)

DUMP_PATHS: frozenset[str] = frozenset(
    {
        "/v1/chat/completions",
        "/v1/messages",
        "/v1/responses",
    }
)


class DumpMiddleware(BaseHTTPMiddleware):
    """Capture request + response bodies for analysis-only endpoints."""

    def __init__(self, app: Any, dump_dir: Path) -> None:
        super().__init__(app)
        self.dump_dir = dump_dir
        self.dump_dir.mkdir(parents=True, exist_ok=True)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.url.path not in DUMP_PATHS or request.method != "POST":
            return await call_next(request)

        t0 = time.perf_counter()

        # Capture and re-stash the request body so the downstream handler
        # can still read it through the standard FastAPI plumbing.
        req_body_bytes = await request.body()

        async def _replay_receive() -> dict[str, Any]:
            return {
                "type": "http.request",
                "body": req_body_bytes,
                "more_body": False,
            }

        request._receive = _replay_receive

        response = await call_next(request)

        # Buffer the response body so we can dump it and still serve it
        # back to the client. Works for both JSON and SSE streams.
        resp_chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            resp_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        resp_body_bytes = b"".join(resp_chunks)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        endpoint_tag = request.url.path.lstrip("/").replace("/", "-")

        # Parse request JSON best-effort.
        try:
            req_json: Any = json.loads(req_body_bytes) if req_body_bytes else None
        except (ValueError, TypeError):
            req_json = {"_raw": req_body_bytes.decode("utf-8", errors="replace")}

        # Response can be JSON (non-streaming) or SSE chunks (streaming).
        media_type = (response.media_type or "").lower()
        is_sse = media_type == "text/event-stream"
        if is_sse:
            resp_repr: Any = {
                "_sse_raw": resp_body_bytes.decode("utf-8", errors="replace"),
            }
        else:
            try:
                resp_repr = json.loads(resp_body_bytes) if resp_body_bytes else None
            except (ValueError, TypeError):
                resp_repr = {"_raw": resp_body_bytes.decode("utf-8", errors="replace")}

        # Routing decision is set by the handler (see api/openai.py and
        # api/anthropic.py). When the handler short-circuited (e.g. 422
        # validation error) the attribute may be missing.
        route_meta = getattr(request.state, "brains_route", None)

        record = {
            "ts": time.time(),
            "endpoint": request.url.path,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "is_sse": is_sse,
            "brains_route": route_meta,
            "request": req_json,
            "response": resp_repr,
        }

        date_tag = time.strftime("%Y-%m-%d")
        out_path = self.dump_dir / f"{endpoint_tag}-{date_tag}.jsonl"
        try:
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str, ensure_ascii=False))
                f.write("\n")
        except OSError as exc:
            logger.warning("brains.dump: failed to write to %s: %s", out_path, exc)

        # Rebuild a fresh Response so the client receives the buffered body.
        # We copy headers but drop Content-Length (will be re-derived) and
        # any Transfer-Encoding: chunked since we're sending the body whole.
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-length", "transfer-encoding"}
        }
        return Response(
            content=resp_body_bytes,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )


def install(app: FastAPI) -> None:
    """Register :class:`DumpMiddleware` iff ``BRAINS_DUMP_DIR`` is set.

    Called once from :mod:`brains.main` after routers are mounted.
    Idempotent: calling twice with the same env var is harmless because
    FastAPI/Starlette deduplicate middleware by class identity at runtime
    (we don't, but the second install is invisible to the caller).
    """
    raw = os.environ.get("BRAINS_DUMP_DIR", "").strip()
    if not raw:
        return

    dump_dir = Path(raw).expanduser().resolve()
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("brains.dump: cannot create %s: %s; dump disabled", dump_dir, exc)
        return

    app.add_middleware(DumpMiddleware, dump_dir=dump_dir)
    logger.info("brains.dump: writing LLM I/O to %s", dump_dir)
