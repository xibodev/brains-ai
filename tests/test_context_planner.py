from brains.context.planner import plan
from brains.router.classifier import classify


def test_plan_docs():
    assert (
        plan(classify([{"role": "user", "content": "latest API docs"}]))["strategy"]
        == "docs_freshness"
    )
