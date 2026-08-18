"""Embeddings + cosine similarity for the semantic retrieval layer.

Talks to Ollama's ``/api/embed`` (batch) so brains can embed repo chunks and
queries with a free local model (e.g. ``nomic-embed-text``) — no API key.
Vectors are stored as packed little-endian float32 BLOBs on ``chunks``.
"""

from __future__ import annotations

import math
import struct

import httpx

from brains.config import settings


class EmbeddingError(RuntimeError):
    pass


def embed_url() -> str:
    return f"{settings.ollama_base_url.rstrip('/')}/api/embed"


def embed_texts(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input, in order."""
    model = model or settings.embed_model
    if not model:
        raise EmbeddingError(
            "no embedding model configured; set embed_model (e.g. nomic-embed-text)"
        )
    if not texts:
        return []
    try:
        resp = httpx.post(
            embed_url(),
            json={"model": model, "input": texts},
            timeout=settings.ollama_timeout_seconds,
        )
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str((resp.json() or {}).get("error", "")).strip()
            except Exception:
                detail = (getattr(resp, "text", "") or "").strip()
            suffix = f": {detail}" if detail else ""
            raise EmbeddingError(f"embedding request failed ({resp.status_code}){suffix}")
        data = resp.json()
    except httpx.TimeoutException as exc:
        raise EmbeddingError("embedding request timed out") from exc
    except EmbeddingError:
        raise
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"embedding request failed: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise EmbeddingError("invalid embedding response payload") from exc

    vectors = data.get("embeddings") if isinstance(data, dict) else None
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise EmbeddingError("embedding response shape mismatch")
    return [[float(x) for x in vec] for vec in vectors]


def embed_text(text: str, model: str | None = None) -> list[float]:
    return embed_texts([text], model=model)[0]


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def score_blobs(query: list[float], blobs: list[bytes]) -> list[float]:
    """Cosine of ``query`` against many packed-float32 vectors.

    Uses numpy when available (one vectorised matmul over the whole matrix —
    ~20x faster than a per-chunk Python loop on a large repo) and falls back to
    pure Python otherwise, so numpy stays an OPTIONAL accelerator, not a required
    dependency. Returns one score per blob, in order; malformed/empty blobs and a
    zero query score 0.0.
    """
    if not blobs:
        return []
    dim = len(query)
    if dim == 0:
        return [0.0] * len(blobs)
    try:
        import numpy as np  # optional accelerator

        q = np.frombuffer(pack_vector(query), dtype="<f4")
        qn = float(np.linalg.norm(q))
        if qn == 0.0:
            return [0.0] * len(blobs)
        # Group rows of the right width into one matrix; handle ragged blobs row-wise.
        good_idx: list[int] = []
        good_blobs: list[bytes] = []
        for i, blob in enumerate(blobs):
            if blob and len(blob) == dim * 4:
                good_idx.append(i)
                good_blobs.append(blob)
        result = [0.0] * len(blobs)
        if good_blobs:
            # One frombuffer over the concatenated bytes + a single reshape is far
            # cheaper than building/stacking thousands of small arrays.
            mat = np.frombuffer(b"".join(good_blobs), dtype="<f4").reshape(len(good_blobs), dim)
            norms = np.linalg.norm(mat, axis=1)
            norms[norms == 0.0] = 1.0
            sims = (mat @ q) / (norms * qn)
            for slot, score in zip(good_idx, sims.tolist(), strict=False):
                result[slot] = float(score)
        return result
    except Exception:
        return [cosine(query, unpack_vector(blob)) if blob else 0.0 for blob in blobs]
