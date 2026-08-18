from brains.router.classifier import classify


def test_classifier():
    assert classify([{"role": "user", "content": "Fix bug"}]).task_type == "code_fix"
