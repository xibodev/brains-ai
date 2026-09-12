"""Blueprint #39: URN graph, blocklist, tiers, dead-code, relations."""

from __future__ import annotations

from brains.context import graph_v2, relations


def test_urn_stable():
    assert graph_v2.urn("function", "src/a.py", "foo") == "function:src/a.py:foo"
    assert graph_v2.urn("class", "src\\b.py", "Bar") == "class:src/b.py:Bar"


def test_blocklist():
    assert "print" in graph_v2.CALL_BLOCKLIST
    assert "useState" in graph_v2.CALL_BLOCKLIST
    target, conf = graph_v2.resolve_call("print", None, [], "src/a.py")
    assert target is None and conf == 0.0


def test_confidence_tiers():
    cands = [
        {"urn": "function:src/a.py:foo", "name": "foo", "path": "src/a.py", "kind": "function"},
        {"urn": "function:src/b.py:foo", "name": "foo", "path": "src/b.py", "kind": "function"},
    ]
    t, c = graph_v2.resolve_call("foo", None, cands, "src/a.py")
    assert (t, c) == ("function:src/a.py:foo", 1.0)
    meth = [
        {
            "urn": "method:src/a.py:C.m",
            "name": "C.m",
            "path": "src/a.py",
            "kind": "method",
            "class_name": "C",
        }
    ]
    t2, c2 = graph_v2.resolve_call("m", "C", meth, "src/a.py")
    assert (t2, c2) == ("method:src/a.py:C.m", 0.8)


def test_build_upsert_and_dead_code():
    files = {
        "src/a.py": "def used():\n    return 1\ndef unused_xyz():\n    return 2\ndef caller():\n    return used()\n",
        "src/b.py": "import os\nprint('hi')\n",
    }
    nodes, edges = graph_v2.build_upsert_rows(files)
    urns = {n["urn"] for n in nodes}
    assert "function:src/a.py:used" in urns
    # print call must not create edge
    assert all(e["dst"] != "print" for e in edges)
    dead = graph_v2.dead_code(
        [
            {"urn": u, "name": u.split(":")[-1], "path": "src/a.py"}
            for u in urns
            if u.startswith("function:")
        ],
        edges,
    )
    assert "function:src/a.py:unused_xyz" in dead
    # rebuild preserves URNs
    nodes2, _ = graph_v2.build_upsert_rows(files)
    assert {n["urn"] for n in nodes2} == urns


def test_cite_edge():
    e = relations.cite_edge("KN-001", "function:src/a.py:used", evidence="verified")
    assert e["relation"] == "cites" and e["confidence"] == 1.0
