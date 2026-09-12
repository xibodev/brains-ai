"""Blueprint #37: specialist adapter contracts."""

from __future__ import annotations

import os
import sys

import pytest

from brains.exec import specialist


def test_prohibited_flags_rejected():
    with pytest.raises(ValueError):
        specialist.validate_argv(["opencode", "run", "--allow-all"])
    with pytest.raises(ValueError):
        specialist.validate_argv(["claude", "--dangerously-skip-permissions"])


def test_scrubbed_env(tmp_path):
    os.environ["TEST_SECRET_TOKEN_XYZ"] = "hunter2"
    env = specialist.scrubbed_env({"KEEP": "1", "MY_TOKEN": "x"})
    assert "MY_TOKEN" not in env
    assert env["KEEP"] == "1"
    sandbox = env["HOME"]
    assert "brains-specialist-sandbox" in sandbox
    for h in specialist.HOST_CREDENTIAL_HINTS:
        assert isinstance(h, str)
    del os.environ["TEST_SECRET_TOKEN_XYZ"]


def test_run_echo_in_worktree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    res = specialist.run_specialist(
        [sys.executable, "-c", "print('hello-specialist')"], worktree=wt, timeout_seconds=30
    )
    assert res["returncode"] == 0
    assert "hello-specialist" in res["head"]
    assert res["worktree"] == str(wt.resolve())
