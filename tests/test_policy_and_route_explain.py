from brains.context.planner import plan
from brains.mcp.tools import explain_route, get_context_pack
from brains.router.classifier import classify


def test_planner_requires_approval_for_large_scans(monkeypatch):
    monkeypatch.setattr(
        "brains.context.planner.settings.control.require_approval_for_large_scans",
        True,
    )
    classification = classify(
        [
            {
                "role": "user",
                "content": "Please scan the entire repo and summarize every file",
            }
        ]
    )
    result = plan(
        classification,
        messages=[
            {
                "role": "user",
                "content": "Please scan the entire repo and summarize every file",
            }
        ],
    )
    assert result["strategy"] == "approval_required"
    assert "large_repo_scan" in result["required_decisions"]


def test_context_pack_labels_external_docs_untrusted(tmp_path):
    (tmp_path / "README.md").write_text("# Hello\n\nlocal content", encoding="utf-8")
    pack = get_context_pack(
        "Review https://example.com/spec and compare with local docs",
        repo_path=str(tmp_path),
    )
    external = [s for s in pack["context_sources"] if s["type"] == "external_docs"][0]
    assert external["trust"] == "untrusted_external"
    assert external["items"][0]["label"] == "untrusted input"


def test_explain_route_includes_provider_model_strategy_and_policy():
    result = explain_route("Fix retry bug in webhook handler")
    assert result["task_type"]
    assert result["provider"]
    assert result["model"]
    assert result["model_tier"]
    assert result["strategy"]
    assert "policy" in result
    assert "required_decisions" in result
