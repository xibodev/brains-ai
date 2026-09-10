"""State-only CLI/MCP work assignments over conftest's disposable real database."""

from __future__ import annotations

import inspect
import json
import subprocess
from dataclasses import replace

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from brains.authz import resolver
from brains.capabilities import CORE_MCP_TOOLS, WITHDRAWN_CLI_GROUPS
from brains.cli.app import app
from brains.control import work_assignments as work
from brains.control.common import normalize_path
from brains.control.sessions import end_session, start_session
from brains.mcp import server as mcp_server
from brains.mcp import tools
from brains.storage.db import SessionLocal
from brains.storage.models import Operator

ACTIONS = ("create", "get", "list", "accept", "settle", "cancel", "retry")
NAMES = {f"work_assignment_{action}" for action in ACTIONS}


@pytest.fixture
def world(tmp_path, monkeypatch):
    workspace = normalize_path(str(tmp_path / "work"))
    owner = "admin"
    creator = start_session(workspace, tool="opencode", operator=owner)["session_id"]
    worker = start_session(workspace, tool="codex", operator=owner)["session_id"]
    principal = resolver.principal_for_operator_slug(owner)
    assert principal is not None
    token = resolver.current_principal.set(principal)

    def forbidden(*args, **kwargs):
        pytest.fail("state-only assignment surface attempted process execution")

    # Guard execution boundaries, not CliRunner's in-process invocation machinery.
    from brains.exec import guard, session_dispatch

    monkeypatch.setattr(guard, "spawn", forbidden)
    monkeypatch.setattr(session_dispatch, "dispatch_owned", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    try:
        yield workspace, creator, worker
    finally:
        resolver.current_principal.reset(token)


@pytest.fixture(params=["cli", "mcp"])
def surface(request):
    def call(action, **kwargs):
        if request.param == "mcp":
            return mcp_server.call_tool(f"brains_work_assignment_{action}", **kwargs)
        args = [f"assignment-{action}"]
        if "code" in kwargs:
            args.append(kwargs.pop("code"))
        aliases = {"workspace_path": "workspace", "session_id": "session", "attempt_id": "attempt"}
        for key, value in kwargs.items():
            args.extend(
                [
                    "--" + aliases.get(key, key).replace("_", "-"),
                    json.dumps(value) if key == "spec" else str(value),
                ]
            )
        result = CliRunner().invoke(app, args)
        if result.exit_code:
            assert result.exception is not None, result.output
            raise result.exception
        return json.loads(result.stdout)

    return call


def create(surface, world, **changes):
    workspace, creator, _ = world
    return surface(
        "create",
        **{
            "workspace_path": workspace,
            "title": "Review synthetic work",
            "spec": {
                "version": 1,
                "objective": "Review the fixture",
                "context": "Synthetic context",
            },
            "session_id": creator,
            "idempotency_key": "surface-create",
            **changes,
        },
    )


def assert_snapshot(snapshot, session):
    # Compare every key, including hashes, provenance, timestamps and attempt metadata.
    assert snapshot == work.get_work_assignment(snapshot["code"], session_id=session)


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "uncertain"])
def test_create_accept_settle_preserves_full_core_snapshot(surface, world, outcome):
    workspace, creator, worker = world
    created = create(surface, world)
    assert created["status"] == "ready"
    assert_snapshot(created, creator)
    assert create(surface, world) == created
    assert surface("get", code=created["code"], session_id=creator) == created
    assert surface("list", workspace_path=workspace, session_id=creator, limit=1) == [created]

    accepted = surface(
        "accept", code=created["code"], session_id=worker, expected_revision=created["revision"]
    )
    assert accepted["status"] == "accepted"
    assert accepted["attempts"][0]["source_session_id"] == worker
    assert accepted["attempts"][0]["tool"] == "codex"
    assert accepted["attempts"][0]["usage"] is None
    assert_snapshot(accepted, creator)
    settled = surface(
        "settle",
        code=created["code"],
        attempt_id=accepted["current_attempt_id"],
        outcome=outcome,
        evidence="Synthetic fixture inspected",
        result="Review report",
        session_id=worker,
        expected_revision=accepted["revision"],
    )
    assert settled["status"] == outcome
    assert settled["attempts"][0]["evidence"] == "Synthetic fixture inspected"
    assert settled["attempts"][0]["result"] == "Review report"
    assert_snapshot(settled, creator)
    assert surface("get", code=created["code"], session_id=creator) == settled
    assert surface("list", workspace_path=workspace, session_id=creator) == [settled]
    if outcome in ("failed", "cancelled"):
        retried = surface(
            "retry", code=created["code"], session_id=creator, expected_revision=settled["revision"]
        )
        assert retried["status"] == "ready"
        assert retried["attempts"] == settled["attempts"]
        assert_snapshot(retried, creator)
    else:
        with pytest.raises(ValueError, match="retry requires conclusive"):
            surface(
                "retry",
                code=created["code"],
                session_id=creator,
                expected_revision=settled["revision"],
            )


def test_cancel_request_requires_settlement_before_retry(surface, world):
    _, creator, worker = world
    created = create(surface, world)
    cancelled = surface("cancel", code=created["code"], session_id=creator, expected_revision=1)
    assert cancelled["status"] == "cancelled"
    assert_snapshot(cancelled, creator)
    ready = surface(
        "retry", code=created["code"], session_id=creator, expected_revision=cancelled["revision"]
    )
    accepted = surface(
        "accept", code=created["code"], session_id=worker, expected_revision=ready["revision"]
    )
    requested = surface(
        "cancel", code=created["code"], session_id=creator, expected_revision=accepted["revision"]
    )
    assert requested["status"] == "cancel_requested"
    assert_snapshot(requested, creator)
    with pytest.raises(ValueError, match="retry requires conclusive"):
        surface(
            "retry",
            code=created["code"],
            session_id=creator,
            expected_revision=requested["revision"],
        )
    settled = surface(
        "settle",
        code=created["code"],
        attempt_id=accepted["current_attempt_id"],
        outcome="cancelled",
        evidence="Cooperative cancellation acknowledged",
        session_id=worker,
        expected_revision=requested["revision"],
    )
    assert settled["attempts"][0]["result"] == ""
    assert_snapshot(settled, creator)


@pytest.mark.parametrize(
    "spec",
    [
        [],
        {"objective": "Review", "spawn": True},
        {"objective": "Review", "provider": "synthetic"},
        {"objective": "Review", "version": 2},
        {"objective": "Review", "max_runtime_seconds": True},
        {"objective": "x" * (work.SPEC_LIMIT + 1)},
    ],
)
def test_invalid_specification_reaches_core_validation(surface, world, spec):
    with pytest.raises(ValueError):
        create(surface, world, spec=spec)
    assert work.list_work_assignments(world[0], session_id=world[1]) == []


def test_stale_revision_and_wrong_accepting_session_fail(surface, world):
    _, creator, worker = world
    created = create(surface, world)
    accepted = surface("accept", code=created["code"], session_id=worker, expected_revision=1)
    with pytest.raises(ValueError, match="stale work assignment revision"):
        surface("cancel", code=created["code"], session_id=creator, expected_revision=1)
    with pytest.raises(ValueError, match="unknown or unavailable"):
        surface(
            "settle",
            code=created["code"],
            attempt_id=accepted["current_attempt_id"],
            outcome="completed",
            evidence="Synthetic evidence",
            session_id=creator,
            expected_revision=accepted["revision"],
        )
    assert_snapshot(accepted, creator)


@pytest.mark.parametrize("limit", [0, 201])
def test_list_is_bounded(surface, world, limit):
    with pytest.raises((ValueError, SystemExit)):
        surface("list", workspace_path=world[0], session_id=world[1], limit=limit)


@pytest.mark.parametrize("changes", [{"outcome": "running"}, {"evidence": ""}])
def test_settlement_rejects_invalid_reports(surface, world, changes):
    _, creator, worker = world
    created = create(surface, world)
    accepted = surface("accept", code=created["code"], session_id=worker, expected_revision=1)
    with pytest.raises(ValueError):
        surface(
            "settle",
            **{
                "code": created["code"],
                "attempt_id": accepted["current_attempt_id"],
                "outcome": "completed",
                "evidence": "Synthetic evidence",
                "session_id": worker,
                "expected_revision": accepted["revision"],
                **changes,
            },
        )
    assert_snapshot(accepted, creator)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("denial", ["foreign", "ended"])
def test_every_surface_requires_live_owned_session(surface, world, action, denial):
    workspace, creator, worker = world
    created = create(surface, world)
    accepted = surface("accept", code=created["code"], session_id=worker, expected_revision=1)
    args = {
        "create": {
            "workspace_path": workspace,
            "title": "New",
            "spec": {"objective": "Review"},
            "idempotency_key": "new-key",
        },
        "list": {"workspace_path": workspace},
        "get": {"code": created["code"]},
        "accept": {"code": created["code"], "expected_revision": accepted["revision"]},
        "cancel": {"code": created["code"], "expected_revision": accepted["revision"]},
        "retry": {"code": created["code"], "expected_revision": accepted["revision"]},
        "settle": {
            "code": created["code"],
            "expected_revision": accepted["revision"],
            "attempt_id": accepted["current_attempt_id"],
            "outcome": "completed",
            "evidence": "Synthetic evidence",
        },
    }[action]
    token = None
    if denial == "ended":
        end_session(worker)
    else:
        principal = resolver.current_principal.get()
        with SessionLocal() as session:
            foreign = session.query(Operator).filter_by(slug="assignment-surface-foreign").first()
            if foreign is None:
                foreign = Operator(slug="assignment-surface-foreign")
                session.add(foreign)
                session.commit()
            foreign_id = foreign.id
        token = resolver.current_principal.set(
            replace(principal, operator_id=foreign_id, is_bootstrap_admin=False)
        )
    try:
        with pytest.raises(ValueError):
            surface(action, session_id=worker, **args)
    finally:
        if token is not None:
            resolver.current_principal.reset(token)


def test_mcp_contract_registration_and_types(monkeypatch):
    assert NAMES <= CORE_MCP_TOOLS
    assert NAMES <= mcp_server.LEAN_TOOLS
    assert set(mcp_server.TOOL_REGISTRY) == CORE_MCP_TOOLS
    registered = {tool.name: tool for tool in mcp_server.mcp._tool_manager.list_tools()}
    for selection in ("full", "lean", ",".join(sorted(NAMES))):
        monkeypatch.setenv("BRAINS_MCP_TOOLS", selection)
        assert set(mcp_server._resolve_active_tools()) >= NAMES
    for action in ACTIONS:
        name = f"work_assignment_{action}"
        wrapper = getattr(tools, f"{name}_tool")
        assert mcp_server.TOOL_REGISTRY[name] is wrapper
        contract = registered[f"brains_{name}"]
        signature = inspect.signature(wrapper)
        assert set(contract.parameters["properties"]) == set(signature.parameters)
        assert set(contract.parameters["required"]) == {
            name for name, param in signature.parameters.items() if param.default is param.empty
        }
        assert "local state-only" in contract.description
        assert signature.parameters["session_id"].kind is inspect.Parameter.KEYWORD_ONLY
        if action != "create":
            core = getattr(work, f"{action}_work_assignment" + ("s" if action == "list" else ""))
            assert signature == inspect.signature(core, eval_str=True)
    spec = registered["brains_work_assignment_create"].parameters["properties"]["spec"]
    assert spec["type"] == "object"
    signature = inspect.signature(tools.work_assignment_create_tool)
    assert list(signature.parameters) == [
        "workspace_path",
        "title",
        "spec",
        "session_id",
        "idempotency_key",
    ]
    assert signature.parameters["spec"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["spec"].annotation is dict
    assert (
        registered["brains_work_assignment_list"].parameters["properties"]["limit"]["type"]
        == "integer"
    )


def test_cli_boundary_and_malformed_json():
    root = get_command(app)
    assert not WITHDRAWN_CLI_GROUPS & set(root.commands)
    for action in ACTIONS:
        command = root.commands[f"assignment-{action}"]
        options = {option for param in command.params for option in param.opts}
        assert "--session" in options
        assert not options & {"--operator", "--spawn", "--provider", "--tool", "--harness"}
        assert "local state-only" in command.help
    result = CliRunner().invoke(
        app,
        [
            "assignment-create",
            "--workspace",
            "/synthetic",
            "--title",
            "Review",
            "--spec",
            "{",
            "--session",
            "ses_synthetic",
            "--idempotency-key",
            "key",
        ],
    )
    assert result.exit_code == 2
    assert "must be valid JSON" in result.output


@pytest.mark.parametrize("field", ["operator", "spawn", "provider", "harness"])
def test_unknown_execution_and_identity_arguments_are_rejected(field):
    with pytest.raises(TypeError):
        mcp_server.call_tool(
            "brains_work_assignment_create",
            workspace_path="/synthetic",
            title="Review",
            spec={"objective": "Review"},
            session_id="ses_synthetic",
            idempotency_key="key",
            **{field: "synthetic"},
        )
    result = CliRunner().invoke(app, ["assignment-create", f"--{field}", "synthetic"])
    assert result.exit_code == 2


@pytest.mark.parametrize("action", ["get", "accept", "settle", "cancel", "retry"])
def test_cli_requires_positional_code_and_session(action):
    assert CliRunner().invoke(app, [f"assignment-{action}", "WA-synthetic"]).exit_code == 2
    assert (
        CliRunner().invoke(app, [f"assignment-{action}", "--session", "ses_synthetic"]).exit_code
        == 2
    )
