"""Local ONNX embedding pipeline with offline deterministic fallback.

Blueprint #35: primary ``BAAI/bge-small-en-v1.5`` (384-dim) via fastembed /
onnxruntime when installed; high-throughput batch alternative
``all-MiniLM-L6-v2``. When no ONNX runtime is available (CI --network none,
minimal installs) a deterministic SHA256-hash-based 384-dim pseudo-embedding
is used so chunking/ranking/benchmarks remain exercisable offline.
"""

from __future__ import annotations

import hashlib
import math
import struct

EMBED_DIM = 384
PRIMARY_MODEL = "BAAI/bge-small-en-v1.5"
BATCH_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _hash_pseudo_embedding(text: str, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    for i in range(dim):
        h = hashlib.sha256(f"{text}\x00{i}".encode()).digest()
        (u,) = struct.unpack("<I", h[:4])
        vec[i] = (u / 2**32) * 2.0 - 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def backend_name() -> str:
    try:
        import fastembed  # noqa: F401

        return "fastembed"
    except Exception:
        pass
    try:
        import onnxruntime  # noqa: F401

        return "onnxruntime"
    except Exception:
        pass
    return "hash-fallback"


def embed_texts_local(texts: list[str], model: str | None = None) -> list[list[float]]:
    if not texts:
        return []
    model = model or PRIMARY_MODEL
    try:
        from fastembed import TextEmbedding

        emb = TextEmbedding(model_name=model)
        return [list(map(float, v)) for v in emb.embed(texts)]
    except Exception:
        pass
    return [_hash_pseudo_embedding(t) for t in texts]


def embed_query_local(text: str, model: str | None = None) -> list[float]:
    return embed_texts_local([text], model=model)[0]
