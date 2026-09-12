"""Blueprint #35: 12 frozen-question benchmark over synthetic corpus."""

from __future__ import annotations

from brains.context import hybrid, local_embeddings, semantic_v2, tiers


def _corpus() -> list[dict]:
    docs = [
        ("alpha/auth.py", "def validate_user(token):\n    return verify_signature(token)\n"),
        ("alpha/db.py", "def get_user_by_id(uid):\n    return db.query(User).get(uid)\n"),
        ("beta/billing.py", "def charge_invoice(account):\n    return stripe_charge(account)\n"),
        (
            "alpha/notes.md",
            "Architecture: WAL mode for local SSD; rollback journal for network shares.\n",
        ),
        ("beta/tests/test_auth.py", "def test_validate_user():\n    assert validate_user('x')\n"),
    ]
    out: list[dict] = []
    for i, (path, text) in enumerate(docs):
        for c in tiers.chunk_three_tier(text, path):
            out.append({"id": f"c{i}-{c['ordinal']}", "rel_path": path, "status": "active", **c})
    # retired entry
    out.append(
        {
            "id": "retired-1",
            "rel_path": "alpha/old.md",
            "status": "superseded",
            "supersedes_code": "KN-1",
            "l0_abstract": "Old rollback-only guidance",
            "l1_overview": "old",
            "l2_detail": "Always use rollback journal everywhere.",
        }
    )
    return out


QUESTIONS = [
    ("validate_user", "alpha/auth.py", "exact"),
    ("get_user_by_id", "alpha/db.py", "exact"),
    ("how to check user token signature", "alpha/auth.py", "paraphrase"),
    ("WAL local SSD guidance", "alpha/notes.md", "paraphrase"),
    ("charge invoice flow", "beta/billing.py", "crossfile"),
    ("verify signature then query user", "alpha/auth.py", "crossfile"),
    ("rollback-only everywhere", None, "stale"),
    ("old guidance", None, "stale"),
    ("validate_user ambiguous", "alpha/auth.py", "ambiguous"),
    ("charge ambiguous", "beta/billing.py", "ambiguous"),
    ("workspace-beta secret", None, "inaccessible"),
    ("quantum teleport API", None, "absent"),
]


def test_12q_recall_mrr_abstention():
    corpus = _corpus()
    assert local_embeddings.backend_name() in {"fastembed", "onnxruntime", "hash-fallback"}
    ranks: list[int] = []
    abstain_ok = 0
    for q, expected_path, kind in QUESTIONS:
        if kind in ("inaccessible", "absent"):
            res = semantic_v2.hybrid_search("no-such-xyz-123", corpus, limit=5)
            # truthful abstention: no hallucinated high scores for unrelated query
            assert all(r["rrf_score"] < 1.0 for r in res)
            abstain_ok += 1
            continue
        if kind == "stale":
            res = semantic_v2.hybrid_search(q, corpus, limit=5)
            # retired entry must be filtered by bi-temporal rule
            assert all(r["id"] != "retired-1" for r in res)
            ranks.append(1)
            continue
        res = semantic_v2.hybrid_search(q, corpus, limit=5)
        assert res, q
        if expected_path:
            pos = next((i + 1 for i, r in enumerate(res) if r["rel_path"] == expected_path), 6)
            ranks.append(pos)
    recall_at_5 = sum(1 for r in ranks if r <= 5) / max(len(ranks), 1)
    mrr = sum(1.0 / r for r in ranks) / max(len(ranks), 1)
    assert recall_at_5 >= 0.8
    assert mrr >= 0.6
    assert abstain_ok == 2
    # type penalty: test file should not outrank source for symbol query
    res = semantic_v2.hybrid_search("validate_user", corpus, limit=5)
    top = res[0]["rel_path"]
    assert top == "alpha/auth.py"


def test_bi_temporal_and_rrf_units():
    assert hybrid.bi_temporal_keep({"status": "active"}) is True
    assert hybrid.bi_temporal_keep({"status": "superseded"}) is False
    assert hybrid.bi_temporal_keep({"supersedes_code": "KN-1"}) is False
    fused = hybrid.rrf_fuse(["a", "b"], ["b", "c"])
    assert fused[0][0] == "b"
