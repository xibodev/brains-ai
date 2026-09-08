"""Hermetic task-owned recovery tests; no native processes or real sleeps."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest

from brains.service import common as service_common
from brains.service import windows, windows_runner
from brains.service.common import ServiceSpec

PROGRAM = r"C:\synthetic venv\Scripts\pythonw.exe"
ARGS = [PROGRAM, "serve-all", "--gateway-port", "8877", "--mcp-port", "9988"]


@pytest.fixture
def runner(monkeypatch):
    state = SimpleNamespace(results=[0], events=[])

    def launch(command, **kwargs):
        state.events.append(("launch", command.copy()))
        assert kwargs == {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        result = state.results.pop(0)
        if isinstance(result, OSError):
            raise result

        def wait():
            state.events.append(("wait", result))
            if isinstance(result, BaseException):
                raise result
            return result

        return SimpleNamespace(wait=wait)

    monkeypatch.setattr(windows_runner.subprocess, "Popen", launch)
    monkeypatch.setattr(
        windows_runner,
        "time",
        SimpleNamespace(sleep=lambda delay: state.events.append(("sleep", delay))),
    )
    return state


@pytest.mark.parametrize("failure", [1, -1, 0x80070001, FileNotFoundError("synthetic-private")])
def test_failure_waits_then_restarts_same_environment(runner, monkeypatch, failure):
    runner.results = [failure, 0]
    monkeypatch.setattr(sys, "executable", r"C:\base Python\pythonw.exe")
    assert windows_runner.main(ARGS) == 0
    command = [PROGRAM, "-m", "brains", *ARGS[1:]]
    expected = [("launch", command)]
    if not isinstance(failure, OSError):
        expected.append(("wait", failure))
    expected += [("sleep", 60), ("launch", command), ("wait", 0)]
    assert runner.events == expected
    assert not runner.results


def test_success_never_restarts(runner):
    assert windows_runner.main(ARGS) == 0
    assert [event[0] for event in runner.events] == ["launch", "wait"]


@pytest.mark.parametrize("failure", [7, PermissionError("synthetic-private")])
def test_retry_budget_is_finite_and_shared_by_launch_failures(runner, monkeypatch, failure, capsys):
    monkeypatch.setattr(windows_runner, "RESTART_COUNT", 2)
    runner.results = [failure] * 3
    assert windows_runner.main(ARGS) == 1
    assert len([event for event in runner.events if event[0] == "launch"]) == 3
    assert [event for event in runner.events if event[0] == "sleep"] == [("sleep", 60)] * 2
    assert not runner.results
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("stage", ["launch", "wait", "sleep"])
@pytest.mark.parametrize("cancellation", [KeyboardInterrupt, SystemExit])
def test_cancellation_never_respawns(runner, monkeypatch, stage, cancellation):
    def cancelled(*_args, **_kwargs):
        raise cancellation

    if stage == "launch":
        monkeypatch.setattr(windows_runner.subprocess, "Popen", cancelled)
    elif stage == "wait":
        runner.results = [cancellation()]
    else:
        runner.results = [1]
        monkeypatch.setattr(windows_runner.time, "sleep", cancelled)
    with pytest.raises(cancellation):
        windows_runner.main(ARGS)
    assert len([event for event in runner.events if event[0] == "launch"]) <= 1


def test_wait_failure_does_not_spawn_over_a_possibly_live_child(runner):
    runner.results = [RuntimeError("synthetic wait failure")]
    with pytest.raises(RuntimeError, match="wait failure"):
        windows_runner.main(ARGS)
    assert [event[0] for event in runner.events] == ["launch", "wait"]


@pytest.mark.parametrize(
    "args",
    [
        [],
        [PROGRAM],
        ["pythonw.exe", "serve-all"],
        [r"C:\venv\python.exe", "serve-all"],
        [PROGRAM, "-c", "pass"],
        [PROGRAM, "-m", "other"],
        [PROGRAM, "service", "stop"],
        [PROGRAM, "serve-all", "--daemon"],
        [PROGRAM, "serve-all", "--help"],
        [PROGRAM, "serve-all", "--gateway-port"],
        [PROGRAM, "serve-all", "--gateway-port", "secret-invalid-port"],
        [PROGRAM, "serve-all", "--gateway-host", "\0"],
    ],
)
def test_invalid_arguments_never_launch_or_log(runner, args, capsys):
    assert windows_runner.main(args) == 2
    assert runner.events == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_rendered_bootstrap_dispatches_runner_with_bound_state(runner, monkeypatch):
    spec = ServiceSpec(program=PROGRAM, state_dir=r"C:\synthetic state\brains")
    encoded = []
    encode = subprocess.list2cmdline

    def capture(args):
        encoded.append(args)
        return encode(args)

    monkeypatch.setattr(windows.subprocess, "list2cmdline", capture)
    monkeypatch.setenv("BRAINS_API_KEY", "synthetic-secret-not-for-xml")
    monkeypatch.setattr(
        service_common,
        "write_pidfile",
        lambda *_args, **_kwargs: pytest.fail("only the supervisor may write its PID"),
    )
    xml = windows.render_task_xml(spec)
    assert "synthetic-secret-not-for-xml" not in xml
    action = encoded[0]
    monkeypatch.setattr(sys, "argv", ["-c", *action[2:]])
    monkeypatch.setenv("BRAINS_STATE_DIR", "synthetic-other-state")
    launched = windows_runner.subprocess.Popen

    def launch(command, **kwargs):
        assert os.environ["BRAINS_STATE_DIR"] == spec.state_dir
        assert command == [PROGRAM, "-m", "brains", "serve-all"]
        return launched(command, **kwargs)

    def dispatch(module, *, run_name, alter_sys):
        assert module == "brains.service.windows_runner"
        assert run_name == "__main__" and alter_sys is True
        assert os.environ["BRAINS_STATE_DIR"] == spec.state_dir
        assert windows_runner.main() == 0

    monkeypatch.setattr(windows_runner.subprocess, "Popen", launch)
    monkeypatch.setattr(runpy, "run_module", dispatch)
    exec(action[1], {})
    assert [event[0] for event in runner.events] == ["launch", "wait"]
    assert windows_runner.RESTART_INTERVAL_SECONDS == 60
    assert windows_runner.RESTART_COUNT == 9999
    assert "<Interval>PT1M</Interval>" in xml
    assert "<Count>9999</Count>" in xml
