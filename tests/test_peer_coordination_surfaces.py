"""Existing-peer CLI/MCP surfaces over conftest's disposable synthetic database."""

from __future__ import annotations

import inspect
import json
import socket
import subprocess
from dataclasses import replace

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from brains.authz import resolver
from brains.authz.principal import Principal
from brains.capabilities import CORE_MCP_TOOLS, WITHDRAWN_CLI_COMMANDS, WITHDRAWN_CLI_GROUPS
from brains.cli.app import app
from brains.control import coordination as peer
from brains.control.common import normalize_path
from brains.control.sessions import end_session, start_session
from brains.mcp import server as mcp_server
from brains.mcp import tools
from brains.storage.db import SessionLocal
from brains.storage.models import Operator

ACTIONS = ("propose", "get", "list", "accept", "advance", "submit", "cancel")
NAMES = {f"coordination_{action}" for action in ACTIONS}


@pytest.fixture
def world(tmp_path, monkeypatch):
    workspace = normalize_path(str(tmp_path / "peers"))
    sessions = [
        start_session(workspace, tool=tool, operator="admin")["session_id"]
        for tool in ("opencode", "codex", "opencode", "codex")
    ]
    principal = resolver.principal_for_operator_slug("admin")
    assert principal is not None
    token = resolver.current_principal.set(principal)

    def forbidden(*args, **kwargs):
        pytest.fail("local peer coordination attempted process execution or network access")

    from brains.exec import guard, session_dispatch

    monkeypatch.setattr(guard, "spawn", forbidden)
    monkeypatch.setattr(session_dispatch, "dispatch_owned", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    try:
        yield workspace, *sessions
    finally:
        resolver.current_principal.reset(token)


@pytest.fixture(params=["cli", "mcp"])
def surface(request):
    def call(action, **kwargs):
        if request.param == "mcp":
            return mcp_server.call_tool(f"brains_coordination_{action}", **kwargs)
        args = [f"coordination-{action}"]
        if "code" in kwargs and action != "propose":
            args.append(kwargs.pop("code"))
        aliases = {"workspace_path": "workspace", "session_id": "session"}
        for key, value in kwargs.items():
            args.extend(
                [
                    "--" + aliases.get(key, key).replace("_", "-"),
                    json.dumps(value) if key in {"spec", "payload"} else str(value),
                ]
            )
        result = CliRunner().invoke(app, args)
        if result.exit_code:
            assert result.exception is not None, result.output
            raise result.exception
        return json.loads(result.stdout)

    return call


def specification(world, **changes):
    return {
        "version": 1,
        "objective": "Review synthetic evidence",
        "context": "Selected inert snapshot",
        "evidence_expectations": "Cite the synthetic probe",
        "participants": [
            {"session_id": world[2], "model": None},
            {"session_id": world[3], "model": "declared-model"},
        ],
        "discussion_rounds": 1,
        **changes,
    }


def propose(surface, world, **changes):
    return surface(
        "propose",
        **{
            "workspace_path": world[0],
            "title": "Synthetic peer review",
            "spec": specification(world),
            "session_id": world[1],
            "idempotency_key": "surface-proposal",
            **changes,
        },
    )


def mutate(surface, action, row, actor, **changes):
    return surface(
        action,
        **{
            "code": row["code"],
            "session_id": actor,
            "version": row["version"],
            "expected_revision": row["revision"],
            **changes,
        },
    )


def report(actor, kind):
    if kind == "final":
        return {"summary": "Synthesis retains disagreement", "evidence": "Synthetic final probe"}
    return {
        "findings": f"private-{actor}-{kind}",
        "evidence": "Synthetic probe",
        "uncertainty": "Not externally verified",
        "dissent": f"objection-{actor}-{kind}",
        "clarifications": ["Which synthetic case?"],
    }


def assert_snapshot(row, actor):
    # Every key matters: flags, counts, provenance, hashes, timestamps and dissent.
    assert row == peer.get_coordination(row["code"], session_id=actor, version=row["version"])


def test_two_participant_lifecycle_preserves_full_filtered_snapshots(surface, world):
    workspace, requester, first, second, _ = world
    row = propose(surface, world)
    assert row["status"] == "planned"
    assert row["incomplete_flag"] and row["blinded"]
    assert_snapshot(row, requester)
    assert propose(surface, world) == row
    assert surface("get", code=row["code"], session_id=requester) == row
    assert surface("list", workspace_path=workspace, session_id=requester, limit=1) == [row]
    stored_peers = {p["session_id"]: p for p in row["specification"]["participants"]}
    assert stored_peers[first] == {"session_id": first, "model": None, "tool": "codex"}
    assert stored_peers[second]["model"] == "declared-model"
    assert stored_peers[second]["tool"] == "opencode"

    for actor in (first, second):
        row = mutate(surface, "accept", row, actor, spec_hash=row["spec_hash"])
        assert_snapshot(row, actor)
    assert row["status"] == "accepted"
    assert mutate(surface, "accept", row, second, spec_hash=row["spec_hash"]) == row
    row = mutate(surface, "advance", row, requester)
    assert row["status"] == "collecting"
    assert_snapshot(row, requester)

    for actor in (first, second):
        row = mutate(
            surface,
            "submit",
            row,
            actor,
            kind="initial",
            payload=report(actor, "initial"),
            idempotency_key="initial",
        )
        assert_snapshot(row, actor)
        assert row["blinded"]
        assert {entry["author_session_id"] for entry in row["contributions"]} == {actor}
        assert (
            mutate(
                surface,
                "submit",
                row,
                actor,
                kind="initial",
                payload=report(actor, "initial"),
                idempotency_key="initial",
            )
            == row
        )
    assert row["counts"]["initial"] == 2
    for actor in (requester, first, second):
        visible = surface("get", code=row["code"], session_id=actor)
        assert_snapshot(visible, actor)
        assert surface("list", workspace_path=workspace, session_id=actor) == [visible]
        assert visible["counts"]["visible_contributions"] == (0 if actor == requester else 1)
        hidden = second if actor != second else first
        assert f"private-{hidden}-initial" not in json.dumps(visible)
        assert f"objection-{hidden}-initial" not in json.dumps(visible)

    row = mutate(surface, "advance", row, requester)
    assert row["status"] == "discussing" and not row["blinded"]
    assert row["counts"]["visible_contributions"] == 2
    assert_snapshot(row, requester)
    for actor in (first, second):
        row = mutate(
            surface,
            "submit",
            row,
            actor,
            kind="discussion",
            payload=report(actor, "discussion"),
            idempotency_key="discussion",
        )
        assert_snapshot(row, actor)
    row = mutate(surface, "advance", row, requester)
    assert row["final_ready"]
    row = mutate(
        surface,
        "submit",
        row,
        requester,
        kind="final",
        payload=report(requester, "final"),
        idempotency_key="final",
    )
    assert row["status"] == "completed" and not row["incomplete_flag"]
    assert row["final"] == report(requester, "final")
    assert len(row["unresolved_dissent"]) == 4
    assert all(entry["resolved"] is False for entry in row["unresolved_dissent"])
    assert_snapshot(row, requester)
    assert surface("get", code=row["code"], session_id=second) == row
    assert surface("list", workspace_path=workspace, session_id=requester) == [row]


def test_replacement_history_cancel_and_stale_fences(surface, world):
    row = propose(surface, world)
    requester = world[1]
    with pytest.raises(ValueError, match="expected_revision requires"):
        propose(surface, world, expected_revision=row["revision"])
    with pytest.raises(ValueError, match="expected_revision"):
        propose(surface, world, code=row["code"], idempotency_key="replace")
    updated = propose(
        surface,
        world,
        code=row["code"],
        expected_revision=row["revision"],
        idempotency_key="replace",
        spec=specification(world, objective="Revised objective"),
    )
    assert updated["version"] == row["version"] + 1
    assert updated["revision"] == row["revision"] + 2
    assert updated["accepted_session_ids"] == [requester]
    assert updated["contributions"] == []
    assert_snapshot(updated, requester)
    historical = surface("get", code=row["code"], session_id=requester, version=row["version"])
    assert historical["status"] == "cancelled"
    assert_snapshot(historical, requester)
    assert surface("list", workspace_path=world[0], session_id=requester) == [updated]
    with pytest.raises(ValueError, match="stale coordination"):
        mutate(
            surface,
            "cancel",
            row,
            requester,
            reason="Stale version",
            expected_revision=updated["revision"],
        )
    with pytest.raises(ValueError, match="stale coordination"):
        mutate(surface, "cancel", updated, requester, reason="Stale revision", expected_revision=0)
    cancelled = mutate(surface, "cancel", updated, requester, reason="Synthetic cancellation")
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancellation_reason"] == "Synthetic cancellation"
    assert_snapshot(cancelled, requester)
    assert (
        mutate(surface, "cancel", cancelled, requester, reason="Synthetic cancellation")
        == cancelled
    )


def test_acceptance_hash_and_requester_authority_are_core_enforced(surface, world):
    row = propose(surface, world)
    with pytest.raises(ValueError, match="spec_hash"):
        mutate(surface, "accept", row, world[2], spec_hash="incorrect")
    for action, extra in (("advance", {}), ("cancel", {"reason": "Not requester"})):
        with pytest.raises(ValueError, match="unknown or unavailable coordination"):
            mutate(surface, action, row, world[2], **extra)
    assert_snapshot(row, world[1])


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("denial", ["anonymous", "foreign", "ended", "unrelated"])
def test_every_surface_keeps_core_session_authorization(surface, world, action, denial):
    row = propose(surface, world)
    workspace, requester, _, _, unrelated = world
    args = {
        "propose": {
            "workspace_path": workspace,
            "title": "Replacement",
            "spec": specification(world),
            "code": row["code"],
            "expected_revision": row["revision"],
            "idempotency_key": "denied",
        },
        "get": {"code": row["code"]},
        "list": {"workspace_path": workspace},
        "accept": {"spec_hash": row["spec_hash"]},
        "advance": {},
        "submit": {
            "kind": "initial",
            "payload": report(requester, "initial"),
            "idempotency_key": "denied",
        },
        "cancel": {"reason": "Denied"},
    }[action]
    if action in {"accept", "advance", "submit", "cancel"}:
        args.update(code=row["code"], version=row["version"], expected_revision=row["revision"])
    actor = requester
    token = None
    if denial == "anonymous":
        token = resolver.current_principal.set(
            Principal(actor_kind="anonymous", actor_id="anonymous", credential_kind="anonymous")
        )
    elif denial == "foreign":
        with SessionLocal() as session:
            foreign = session.query(Operator).filter_by(slug="peer-surface-foreign").first()
            if foreign is None:
                foreign = Operator(slug="peer-surface-foreign")
                session.add(foreign)
                session.commit()
            foreign_id = foreign.id
        token = resolver.current_principal.set(
            replace(
                resolver.current_principal.get(),
                operator_id=foreign_id,
                is_bootstrap_admin=False,
            )
        )
    elif denial == "ended":
        end_session(requester)
    else:
        actor = unrelated
    try:
        if denial == "unrelated" and action == "list":
            assert surface(action, session_id=actor, **args) == []
        else:
            with pytest.raises(ValueError):
                surface(action, session_id=actor, **args)
    finally:
        if token is not None:
            resolver.current_principal.reset(token)


@pytest.mark.parametrize("limit", [0, 201, True])
def test_list_limit_is_bounded_and_not_boolean(surface, world, limit):
    with pytest.raises((ValueError, SystemExit)):
        surface("list", workspace_path=world[0], session_id=world[1], limit=limit)


@pytest.mark.parametrize("field", ["version", "expected_revision"])
def test_boolean_revision_parameters_are_rejected(surface, world, field):
    row = propose(surface, world)
    with pytest.raises((ValueError, SystemExit)):
        mutate(surface, "cancel", row, world[1], reason="Invalid number", **{field: True})
    assert_snapshot(row, world[1])


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"discussion_rounds": True},
        {"spawn": True},
        {"provider": "synthetic"},
    ],
)
def test_spec_validation_remains_in_core(surface, world, changes):
    with pytest.raises(ValueError):
        propose(surface, world, spec=specification(world, **changes))
    assert peer.list_coordinations(world[0], session_id=world[1]) == []


@pytest.mark.parametrize("action,field", [("propose", "spec"), ("submit", "payload")])
def test_cli_reports_malformed_json(action, field):
    args = [f"coordination-{action}"]
    if action == "propose":
        args += ["--workspace", "/synthetic", "--title", "Review"]
    else:
        args += ["PC-synthetic", "--kind", "initial", "--version", "1", "--expected-revision", "0"]
    args += [f"--{field}", "{", "--session", "ses_synthetic", "--idempotency-key", "key"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert "must be valid JSON" in result.output


def test_mcp_exact_registration_signatures_and_schema(monkeypatch):
    assert len(CORE_MCP_TOOLS) == 89
    assert NAMES <= CORE_MCP_TOOLS
    assert NAMES <= mcp_server.LEAN_TOOLS
    assert set(mcp_server.TOOL_REGISTRY) == CORE_MCP_TOOLS
    registered = {tool.name: tool for tool in mcp_server.mcp._tool_manager.list_tools()}
    assert {f"brains_{name}" for name in NAMES} <= set(mcp_server.list_tools())
    for selection in ("full", "lean", ",".join(sorted(NAMES))):
        monkeypatch.setenv("BRAINS_MCP_TOOLS", selection)
        assert set(mcp_server._resolve_active_tools()) >= NAMES
    for action in ACTIONS:
        name = f"coordination_{action}"
        wrapper = getattr(tools, f"{name}_tool")
        core = getattr(peer, f"{action}_coordination" + ("s" if action == "list" else ""))
        signature = inspect.signature(wrapper)
        expected = inspect.signature(core, eval_str=True)
        if action == "propose":
            expected = expected.replace(
                parameters=[
                    param.replace(name="spec") if param.name == "specification" else param
                    for param in expected.parameters.values()
                ]
            )
        assert signature == expected
        assert mcp_server.TOOL_REGISTRY[name] is wrapper
        contract = registered[f"brains_{name}"]
        assert set(contract.parameters["properties"]) == set(signature.parameters)
        assert set(contract.parameters["required"]) == {
            key for key, param in signature.parameters.items() if param.default is param.empty
        }
        properties = contract.parameters["properties"]
        for field, param in signature.parameters.items():
            schema = properties[field]
            if param.annotation in (int | None, str | None):
                assert {part["type"] for part in schema["anyOf"]} == {
                    "null",
                    "integer" if param.annotation == int | None else "string",
                }
                assert schema["default"] is None
            else:
                assert (
                    schema["type"]
                    == {str: "string", dict: "object", int: "integer"}[param.annotation]
                )
        assert signature.parameters["session_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "declarations, not model calls" in registered["brains_coordination_propose"].description


@pytest.mark.parametrize("action", ACTIONS)
def test_mcp_wrapper_returns_core_object_unchanged(monkeypatch, action):
    wrapper = getattr(tools, f"coordination_{action}_tool")
    core_name = f"{action}_coordination" + ("s" if action == "list" else "")
    core_signature = inspect.signature(getattr(peer, core_name))
    result = [{"complete": "snapshot"}] if action == "list" else {"complete": "snapshot"}
    signature = inspect.signature(wrapper)
    args = {
        name: ({} if param.annotation is dict else 1 if param.annotation is int else "synthetic")
        for name, param in signature.parameters.items()
        if param.default is param.empty
    }
    expected = dict(signature.bind(**args).arguments)
    for name, param in signature.parameters.items():
        if name not in expected:
            expected[name] = param.default
    if action == "propose":
        expected["specification"] = expected.pop("spec")

    def capture(*positional, **keywords):
        assert core_signature.bind(*positional, **keywords).arguments == expected
        return result

    monkeypatch.setattr(tools, core_name, capture)
    assert mcp_server.call_tool(f"brains_coordination_{action}", **args) is result


def test_cli_exact_options_and_required_fences():
    root = get_command(app)
    assert not (WITHDRAWN_CLI_COMMANDS | WITHDRAWN_CLI_GROUPS) & set(root.commands)
    expected = {
        "propose": {
            "workspace",
            "title",
            "spec",
            "session",
            "idempotency-key",
            "code",
            "expected-revision",
        },
        "get": {"session", "version"},
        "list": {"workspace", "session", "limit"},
        "accept": {"session", "version", "expected-revision", "spec-hash"},
        "advance": {"session", "version", "expected-revision"},
        "submit": {"kind", "payload", "idempotency-key", "session", "version", "expected-revision"},
        "cancel": {"reason", "session", "version", "expected-revision"},
    }
    for action in ACTIONS:
        command = root.commands[f"coordination-{action}"]
        options = {opt for param in command.params for opt in param.opts if opt.startswith("--")}
        assert options == {f"--{name}" for name in expected[action]}
        for param in command.params:
            if param.name == "session" or (
                action in {"accept", "advance", "submit", "cancel"}
                and param.name in {"version", "expected_revision"}
            ):
                assert param.required
            if param.name in {"version", "expected_revision", "limit"}:
                assert not getattr(param, "is_flag", False)
        if action not in {"propose", "list"}:
            assert command.params[0].name == "code" and command.params[0].required
            result = CliRunner().invoke(app, [f"coordination-{action}", "PC-synthetic"])
            assert result.exit_code == 2


@pytest.mark.parametrize(
    "field", ["operator", "callerop", "spawn", "worker", "provider", "model", "harness"]
)
def test_execution_and_identity_override_flags_are_absent(field):
    with pytest.raises(TypeError):
        mcp_server.call_tool(
            "brains_coordination_propose",
            workspace_path="/synthetic",
            title="Review",
            spec={},
            session_id="ses_synthetic",
            idempotency_key="key",
            **{field: "synthetic"},
        )
    result = CliRunner().invoke(app, ["coordination-propose", f"--{field}", "synthetic"])
    assert result.exit_code == 2
