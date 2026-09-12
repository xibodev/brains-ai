"""Dual vector storage engine: Alibaba zvec (primary) + NumPy fallback.

Blueprint #35: in-process HNSW with RocksDB WAL when ``zvec`` is installed,
pure-NumPy exact scan otherwise. No new required dependency: ``zvec`` is an
optional accelerator. Batch inserts are chunked to <=1024 docs per call
(zvec hard limit). Exclusive OS LOCK semantics are respected by using
short-lived writer sessions.
"""

from __future__ import annotations

import contextlib
import struct
from pathlib import Path


def zvec_available() -> bool:
    try:
        import zvec  # noqa: F401

        return True
    except Exception:
        return False


def pack_f32(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_f32(blob: bytes) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class NumpyVectorStore:
    """Exact in-memory cosine store. Zero native deps beyond optional numpy."""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.ids: list[str] = []
        self.vecs: list[list[float]] = []

    def insert(self, items: list[tuple[str, list[float]]]) -> None:
        for doc_id, vec in items:
            if len(vec) != self.dim:
                raise ValueError("dimension mismatch")
            if doc_id in self.ids:
                idx = self.ids.index(doc_id)
                self.vecs[idx] = list(vec)
            else:
                self.ids.append(doc_id)
                self.vecs.append(list(vec))

    def query(self, vector: list[float], topk: int = 5) -> list[tuple[str, float]]:
        if not self.ids or not vector:
            return []
        try:
            import numpy as np

            mat = np.array(self.vecs, dtype="<f4")
            q = np.array(vector, dtype="<f4")
            qn = float(np.linalg.norm(q))
            if qn == 0.0:
                return [(i, 0.0) for i in self.ids[:topk]]
            norms = np.linalg.norm(mat, axis=1)
            norms[norms == 0.0] = 1.0
            sims = (mat @ q) / (norms * qn)
            order = sims.argsort()[::-1][:topk]
            return [(self.ids[int(i)], float(sims[int(i)])) for i in order]
        except Exception:
            import math

            def cos(a: list[float], b: list[float]) -> float:
                dot = sum(x * y for x, y in zip(a, b, strict=False))
                na = math.sqrt(sum(x * x for x in a))
                nb = math.sqrt(sum(y * y for y in b))
                if na == 0.0 or nb == 0.0:
                    return 0.0
                return dot / (na * nb)

            scored = [(i, cos(vector, v)) for i, v in zip(self.ids, self.vecs, strict=False)]
            scored.sort(key=lambda t: -t[1])
            return scored[:topk]

    def __len__(self) -> int:
        return len(self.ids)


class ZvecVectorStore:
    """Thin wrapper over zvec collection with batching + read-only support."""

    def __init__(self, path: str | Path, dim: int, *, read_only: bool = False) -> None:
        import zvec

        self._zvec = zvec
        self.dim = dim
        self.path = str(path)
        schema = zvec.CollectionSchema(
            name="brains_chunks",
            vectors=zvec.VectorSchema("embedding", zvec.DataType.VECTOR_FP32, dim),
        )
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            self.col = zvec.open(self.path, read_only=True)
        else:
            try:
                self.col = zvec.create_and_open(path=self.path, schema=schema)
            except Exception:
                self.col = zvec.open(self.path, read_only=False)

    def insert(self, items: list[tuple[str, list[float]]]) -> None:
        for i in range(0, len(items), 1024):
            batch = items[i : i + 1024]
            docs = [self._zvec.Doc(id=doc_id, vectors={"embedding": vec}) for doc_id, vec in batch]
            self.col.insert(docs)
        with contextlib.suppress(Exception):
            self.col.flush()

    def query(self, vector: list[float], topk: int = 5) -> list[tuple[str, float]]:
        res = self.col.query(self._zvec.Query(field_name="embedding", vector=vector), topk=topk)
        return [(r["id"], float(r.get("score", 0.0))) for r in res]

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.col.close()


def open_vector_store(path: str | Path, dim: int, *, read_only: bool = False):
    """Open zvec when available, else in-memory NumPy store."""
    if zvec_available():
        return ZvecVectorStore(path, dim, read_only=read_only)
    return NumpyVectorStore(dim)
