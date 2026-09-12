"""Semantic v2: hybrid lexical+vector retrieval over three-tier chunks.

Ties lookup_workspace (lexical), local ONNX embeddings + dual vector store
(semantic), RRF fusion, type penalties, and bi-temporal filtering into one
deterministic search used by the 12-question benchmark. Operates on in-memory
chunk dicts so benchmarks run without DB migrations.
"""

from __future__ import annotations

from . import hybrid, local_embeddings, vector_store


def index_chunks(chunk_dicts: list[dict], dim: int = 384):
    texts = [c.get("l0_abstract", "") + " " + c.get("l2_detail", "") for c in chunk_dicts]
    vecs = local_embeddings.embed_texts_local(texts)
    store = vector_store.open_vector_store(":memory:", dim)
    if isinstance(store, vector_store.NumpyVectorStore):
        store.insert([(c["id"], v) for c, v in zip(chunk_dicts, vecs, strict=False)])
    else:
        try:
            store.insert([(c["id"], v) for c, v in zip(chunk_dicts, vecs, strict=False)])
        except Exception:
            store = vector_store.NumpyVectorStore(dim)
            store.insert([(c["id"], v) for c, v in zip(chunk_dicts, vecs, strict=False)])
    return store, vecs


def hybrid_search(
    query: str,
    chunk_dicts: list[dict],
    *,
    limit: int = 5,
    lexical_hits: list[str] | None = None,
) -> list[dict]:
    by_id = {c["id"]: c for c in chunk_dicts}
    if lexical_hits is None:
        ql = query.lower()
        lexical_hits = [c["id"] for c in chunk_dicts if ql and ql in c.get("l2_detail", "").lower()]
    qvec = local_embeddings.embed_query_local(query)
    store, _ = index_chunks(chunk_dicts)
    sem_ranked = [doc for doc, _ in store.query(qvec, topk=max(limit * 3, len(chunk_dicts)))]
    fused = hybrid.rrf_fuse(lexical_hits, sem_ranked)
    out: list[dict] = []
    for doc_id, score in fused:
        row = by_id.get(doc_id)
        if row is None or not hybrid.bi_temporal_keep(row):
            continue
        adj = hybrid.apply_type_adjustment(doc_id, row.get("rel_path"), score)
        out.append({**row, "rrf_score": round(adj, 6)})
        if len(out) >= limit:
            break
    return out
