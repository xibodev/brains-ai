"""Brains CLI — Typer app + every verb the binary exposes."""

import json
import sys
from pathlib import Path
from typing import Any

import typer
import uvicorn

from brains.context.docs_indexer import index_docs, search_docs
from brains.context.freshness import check_source
from brains.context.graph_viz import graph_export
from brains.context.planner import plan
from brains.context.repo_indexer import (
    index_repo,
    index_repo_persisted,
    search_repo,
    search_repo_persisted,
)
from brains.control.claims import (
    claim_workspace,
    list_workspace_claims,
    release_workspace,
)
from brains.control.decisions import (
    file_decision_request,
    list_open_decisions,
    resolve_decision,
)
from brains.control.durable_mailbox import (
    create_managed_agent_mailbox,
    ensure_operator_mailboxes,
    extract_native_tool_session_id,
    list_phonebook,
    lookup_mailbox,
    read_mailbox_binding_file,
    reconcile_managed_mailbox_bindings,
    recover_managed_agent_mailbox_binding,
    register_agent_mailbox,
    revoke_managed_agent_mailbox_binding,
    rotate_managed_agent_mailbox_binding,
)
from brains.control.events import append_event, list_events
from brains.control.handoffs import (
    clear_handoff,
    list_handoffs,
    pick_handoff,
    set_handoff,
)
from brains.control.jobs import list_jobs, run_job
from brains.control.learn import propose_from_history
from brains.control.mailbox import read_messages, send_message
from brains.control.patterns import (
    approve_pattern,
    list_patterns,
    propose_pattern,
    use_pattern,
)
from brains.control.recurring import (
    create_recurring_task,
    fire_recurring_task,
    list_recurring_tasks,
    set_recurring_enabled,
)
from brains.control.sessions import end_session, list_sessions, start_session
from brains.control.snapshots import capture_snapshot, latest_snapshot
from brains.control.state import get_state
from brains.control.tasks import (
    claim_task,
    complete_task,
    create_task,
    handoff_task,
    list_tasks,
    release_task,
)
from brains.control.tool_registry import (
    list_registered_tools,
    register_tool,
    verify_tool,
)
from brains.control.views import refresh_views
from brains.main import app as fastapi_app
from brains.router.classifier import classify
from brains.storage.repositories import list_traces


def _version_string() -> str:
    from brains import __version__

    return __version__


def _version_callback(value: bool) -> None:
    if value:
        print(_version_string())
        raise typer.Exit()


app = typer.Typer()
jobs_app = typer.Typer()
admin_key_app = typer.Typer(help="Manage the admin/API key persisted in ~/.brains/admin-key.")
operator_app = typer.Typer(
    help="Manage operators — named principals that own sessions. "
    "Layer 1 of the multi-operator model."
)
workspace_app = typer.Typer(
    help="Manage workspace visibility and per-operator membership. "
    "Layer 2 of the multi-operator model."
)
service_app = typer.Typer(
    help="Install brains serve-all as a user OS service that autostarts at "
    "login and restarts on failure (Windows Task Scheduler / macOS launchd / "
    "Linux systemd --user)."
)
credentials_app = typer.Typer(
    help="Inspect, revoke and diagnose the HTTP credentials this install "
    "accepts. Every accepted key resolves to one principal; raw secrets are "
    "never stored or printed."
)
mailbox_app = typer.Typer(
    help="Register durable agent mailboxes and inspect the authorized phonebook."
)
app.add_typer(jobs_app, name="jobs")
app.add_typer(admin_key_app, name="admin-key")
app.add_typer(operator_app, name="operator")
app.add_typer(workspace_app, name="workspace")
app.add_typer(service_app, name="service")
app.add_typer(credentials_app, name="credentials")
app.add_typer(mailbox_app, name="mailbox")

daemon_app = typer.Typer(
    help="Run the brains daemon — one process per machine that detects coding "
    "CLIs, registers them as runtimes on the hub, heartbeats, and polls for "
    "assignments to spawn (gated) via exec.runner."
)
app.add_typer(daemon_app, name="daemon")

# `run` is defined in brains.cli.run and registered as a command (not a
# sub-Typer) so the syntax stays `brains-ai run <tool>` rather than
# `brains-ai run <subcommand>`. allow_extra_args + ignore_unknown_options
# let `brains-ai run claude --resume` forward `--resume` to claude
# verbatim instead of having Typer reject it.
from brains.cli.run import run_tool_cli as _run_tool_cli  # noqa: E402

app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)(_run_tool_cli)


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the brains version and exit.",
    ),
):
    """Brains — local-first control plane for AI coding agents."""
    return


def _print_json(value):
    print(json.dumps(value, indent=2, default=str))


def _require_experimental_cli(label: str) -> None:
    """Refuse an experimental command unless BRAINS_MCP_EXPERIMENTAL opts in.

    Mirrors the CLI's one-line ``error: <msg>`` style and exits 2 so scripts
    compose; the message always names the enabling switch.
    """
    from brains.experimental import ExperimentalDisabledError, require_experimental

    try:
        require_experimental(label)
    except ExperimentalDisabledError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None


@app.command("version")
def version_cli():
    """Print brains version + schema version + installed extras."""
    from brains.config import RUNTIME_OVERLAY_SCHEMA_VERSION
    from brains.extras import installed_extras

    out: dict[str, object] = {
        "version": _version_string(),
        "runtime_overlay_schema_version": RUNTIME_OVERLAY_SCHEMA_VERSION,
        "extras": installed_extras(),
    }
    try:
        from brains.storage.migrations import current_schema_versions

        out["db_schema_versions"] = current_schema_versions()
    except Exception as exc:  # noqa: BLE001 — keep `version` robust on fresh installs
        out["db_schema_versions"] = None
        out["db_schema_versions_error"] = f"{type(exc).__name__}: {exc}"
    _print_json(out)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8787):
    uvicorn.run(fastapi_app, host=host, port=port)


@app.command("prune-traces")
def prune_traces_cli(
    max_rows: int = typer.Option(
        -1,
        help="Keep only this many most-recent traces (-1 = use configured "
        "trace_retention_max_rows). 0 keeps all rows.",
    ),
    max_payload_bytes: int = typer.Option(
        -1,
        help="Truncate surviving payloads to this many bytes (-1 = use "
        "configured trace_max_payload_bytes). 0 disables truncation.",
    ),
    vacuum: bool = typer.Option(
        True, help="Run VACUUM after pruning to reclaim freed pages on disk."
    ),
):
    """One-shot cleanup of an oversized traces table.

    The traces table stores a redacted copy of each gateway request; on a
    busy brain it can dominate the DB. Per-write retention bounds new rows,
    but an already-bloated table needs this backfill. Safe to run live.
    """
    from brains.storage.db import SessionLocal
    from brains.storage.repositories import prune_traces_now

    kw: dict[str, int] = {}
    if max_rows >= 0:
        kw["max_rows"] = max_rows
    if max_payload_bytes >= 0:
        kw["max_payload_bytes"] = max_payload_bytes
    result: dict[str, Any] = prune_traces_now(**kw)
    if vacuum:
        try:
            from sqlalchemy import text

            with SessionLocal() as session:
                session.execute(text("VACUUM"))
                session.commit()
            result["vacuumed"] = True
        except Exception as exc:  # noqa: BLE001 — VACUUM is best-effort
            result["vacuumed"] = False
            result["vacuum_error"] = f"{type(exc).__name__}: {exc}"
    _print_json(result)


@app.command("serve-all")
def serve_all_cli(
    gateway_host: str = "127.0.0.1",
    gateway_port: int = 8787,
    mcp_port: int = 9877,
    mcp_scheduler_interval: int = 60,
    no_gateway: bool = False,
    no_mcp: bool = False,
):
    """Supervise gateway + MCP server in one process (restart-on-crash).

    The MCP server is what agent CLIs/IDEs connect to, so it is included by
    default. Pass --no-mcp to leave it out. Its bind host follows
    BRAINS_MCP_BIND / BRAINS_MCP_ALLOW_PUBLIC (defaults to loopback).
    """
    from brains.control.supervisor import run as supervisor_run

    argv: list[str] = []
    if no_gateway:
        argv.append("--no-gateway")
    if no_mcp:
        argv.append("--no-mcp")
    argv += ["--gateway-host", gateway_host, "--gateway-port", str(gateway_port)]
    argv += ["--mcp-port", str(mcp_port)]
    argv += ["--mcp-scheduler-interval", str(mcp_scheduler_interval)]
    raise SystemExit(supervisor_run(argv))


@app.command("mcp")
def mcp_cli(
    mode: str = typer.Option(
        "streamable-http",
        "--mode",
        help="Transport: 'streamable-http' (hosted, default), 'stdio', or legacy 'sse'.",
    ),
    port: int = typer.Option(9877, "--port", help="Port for HTTP transport modes."),
    scheduler_interval: int = typer.Option(
        60,
        "--scheduler-interval",
        help="Seconds between recurring-task scheduler ticks (hosted HTTP modes).",
    ),
):
    """Run the Brains MCP server so agent CLIs/IDEs can connect.

    Streamable HTTP is the hosted default at http://127.0.0.1:<port>/mcp.
    Use ``stdio`` for tools that explicitly spawn the server as a subprocess;
    ``sse`` remains an explicit legacy compatibility mode. The HTTP bind host
    is driven by BRAINS_MCP_BIND / BRAINS_MCP_ALLOW_PUBLIC (loopback by default).
    """
    if mode not in {"streamable-http", "stdio", "sse"}:
        raise typer.BadParameter("mode must be 'streamable-http', 'stdio', or legacy 'sse'")
    from brains.mcp.server import run_mcp_server

    run_mcp_server(mode=mode, port=port, scheduler_interval=scheduler_interval)


@app.command("up")
def up_cli(
    gateway_host: str = "127.0.0.1",
    gateway_port: int = 8787,
    mcp_port: int = 9877,
    no_gateway: bool = False,
    no_mcp: bool = False,
):
    """Zero-to-running: init the DB + workspace, then supervise the stack.

    Equivalent to `brains-ai init` followed by `brains-ai serve-all`.
    Idempotent — safe to re-run.
    """
    from brains.api.admin_key import ensure_admin_key
    from brains.control.sessions import register_workspace
    from brains.control.supervisor import run as supervisor_run
    from brains.storage.migrations import init_db

    init_db()
    register_workspace(".")
    ensure_admin_key(print_banner=True)

    argv: list[str] = []
    if no_gateway:
        argv.append("--no-gateway")
    if no_mcp:
        argv.append("--no-mcp")
    argv += ["--gateway-host", gateway_host, "--gateway-port", str(gateway_port)]
    argv += ["--mcp-port", str(mcp_port)]
    raise SystemExit(supervisor_run(argv))


def _canonical_db_url() -> str:
    """The DB URL to embed when wiring stdio MCP servers.

    Returns ``settings.db_url``. The ``Settings.db_url`` validator already
    rewrites the bare ``sqlite:///brains.db`` default to the absolute
    per-machine path under ``BRAINS_STATE_DIR`` (or ``~/.brains``), so
    every entry point — the bare CLI, the HTTP MCP server, stdio MCP children
    spawned by agents — agrees on one shared brain.

    The literal-string fallback below is kept as defence in depth in case
    a future caller constructs ``settings`` outside the normal pydantic
    pipeline (e.g. tests using ``SimpleNamespace``); the validator is the
    canonical source of truth.
    """

    from brains.config import DEFAULT_DB_URL, _canonical_default_db_url, settings

    url = settings.db_url
    if url == DEFAULT_DB_URL:
        return _canonical_default_db_url()
    return url


def _effective_wire_api_key(*, create: bool) -> str:
    """Resolve the effective MCP credential; create only when explicitly requested."""

    from brains.api.admin_key import ensure_admin_key, read_persisted_key
    from brains.config import settings

    if create:
        key, _ = ensure_admin_key(print_banner=False)
        return key
    return settings.api_key or read_persisted_key() or ""


@app.command("wire")
def wire_cli(
    transport: str = typer.Option(
        "streamable-http",
        "--transport",
        help="MCP transport: 'streamable-http' (default), legacy 'sse', or 'stdio'.",
    ),
    tool: list[str] = typer.Option(
        [],
        "--tool",
        help="Limit to specific tool(s): copilot-cli, claude-code, codex, opencode. Repeatable.",
    ),
    url: str | None = typer.Option(
        None, "--url", help="HTTP MCP URL (default http://127.0.0.1:<port>/mcp)."
    ),
    port: int = typer.Option(9877, "--port", help="MCP port used when --url is not given."),
    no_rules: bool = typer.Option(
        False, "--no-rules", help="Only wire the MCP entry; skip the instruction rule."
    ),
    force: bool = typer.Option(
        False, "--force", help="Wire even tools whose config dir is absent."
    ),
    show_status: bool = typer.Option(
        False, "--status", help="Report current wiring state and exit. No changes."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change; write nothing."),
):
    """Register brains into installed agentic tools (Copilot CLI, Claude Code, Codex, OpenCode).

    Global by default — brains is a multi-session coordination tool, so it
    wires into the user-level config of every detected tool. Idempotent and
    reversible (`brains-ai unwire`); each file is backed up before any edit.
    """
    import sys

    from brains import wire as wire_mod
    from brains.mcp.transport import (
        MCP_MODE_SSE,
        MCP_MODE_STDIO,
        MCP_MODE_STREAMABLE_HTTP,
        mcp_http_url,
    )

    home = Path.home()
    if show_status:
        _print_json(wire_mod.status(home))
        return
    if transport not in {MCP_MODE_STREAMABLE_HTTP, MCP_MODE_SSE, MCP_MODE_STDIO}:
        raise typer.BadParameter("transport must be 'streamable-http', 'stdio', or legacy 'sse'")

    default_url = (
        f"http://127.0.0.1:{port}/sse" if transport == MCP_MODE_SSE else mcp_http_url(port=port)
    )

    ctx = wire_mod.WireContext(
        transport=transport,
        url=url or default_url,
        python=sys.executable,
        db_url=_canonical_db_url(),
    )
    if transport != MCP_MODE_STDIO:
        ctx.api_key = _effective_wire_api_key(create=False)

    report = wire_mod.wire(
        home,
        ctx,
        tools=list(tool) or None,
        rules=not no_rules,
        force=force,
        dry_run=dry_run,
    )
    _print_json(report)
    if report.get("ok") is False:
        raise typer.Exit(code=1)


@app.command("unwire")
def unwire_cli(
    tool: list[str] = typer.Option([], "--tool", help="Limit to specific tool(s). Repeatable."),
    no_rules: bool = typer.Option(
        False,
        "--no-rules",
        help="Only remove the MCP entry; leave the instruction rule.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change; write nothing."),
):
    """Remove brains MCP entries and rule blocks from agentic tools."""
    from brains import wire as wire_mod

    report = wire_mod.unwire(
        Path.home(),
        tools=list(tool) or None,
        rules=not no_rules,
        dry_run=dry_run,
    )
    _print_json(report)


# ---------------------------------------------------------------------------
# OS service (autostart serve-all)
# ---------------------------------------------------------------------------


@service_app.command("install")
def service_install_cli(
    gateway_port: int | None = typer.Option(
        None,
        "--gateway-port",
        min=1,
        max=65535,
        help="Gateway port. Omit to reuse the persisted port or select a safe local default.",
    ),
    mcp_port: int | None = typer.Option(
        None,
        "--mcp-port",
        min=1,
        max=65535,
        help="MCP HTTP port. Omit to reuse the persisted port or default to 9877.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the unit definition + commands; write nothing."
    ),
):
    """Register brains serve-all as a user service (autostart + restart).

    Runs as the logged-in user so HOME, the canonical per-machine DB, and the
    github_copilot OAuth cache all resolve. Idempotent — re-running re-writes
    the unit. Use `brains-ai service uninstall` to remove it.
    """
    from brains import service as service_mod

    if not service_mod.supported():
        raise typer.BadParameter(
            f"No service backend for platform {service_mod.current_platform()!r} "
            "(supported: windows, macos, linux)."
        )
    _print_json(
        service_mod.install(
            dry_run=dry_run,
            gateway_port=gateway_port,
            mcp_port=mcp_port,
        )
    )


@service_app.command("uninstall")
def service_uninstall_cli(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be removed; change nothing."
    ),
):
    """Stop and remove the brains serve-all autostart service."""
    from brains import service as service_mod

    _print_json(service_mod.uninstall(dry_run=dry_run))


@service_app.command("status")
def service_status_cli():
    """Report whether the autostart service is installed + its run state."""
    from brains import service as service_mod

    _print_json(service_mod.status())


@service_app.command("start")
def service_start_cli():
    """Start the installed service now."""
    from brains import service as service_mod

    _print_json(service_mod.start())


@service_app.command("stop")
def service_stop_cli():
    """Stop the running service (and reap the supervised child tree)."""
    from brains import service as service_mod

    _print_json(service_mod.stop())


@service_app.command("restart")
def service_restart_cli():
    """Restart the service."""
    from brains import service as service_mod

    _print_json(service_mod.restart())


@service_app.command("logs")
def service_logs_cli(
    lines: int = typer.Option(40, "--lines", "-n", help="Tail the last N log lines."),
):
    """Print the tail of the supervisor log (~/.brains/sessions/service.log)."""
    from brains.service.common import state_dir

    log = state_dir() / "sessions" / "service.log"
    if not log.exists():
        typer.echo(f"no log yet at {log}")
        return
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    typer.echo("\n".join(tail))


# ---------------------------------------------------------------------------
# GitHub Copilot provider auth
# ---------------------------------------------------------------------------


@app.command("copilot-login")
def copilot_login_cli() -> None:
    """Run the GitHub device-code flow to authorize the github_copilot provider.

    Prints the user code + verification URL, polls until the user
    authorizes, then caches the OAuth token at
    ``~/.brains/cache/github_copilot_oauth.json`` (0600 best-effort).
    The provider's session-token exchange runs on first chat call.

    Note: GitHub Copilot is licensed for code suggestions in editors. Using it
    as a general gateway provider is a personal-use grey area, not a sanctioned
    public API \u2014 keep it on your own loopback gateway and do not expose it as a
    shared/hosted relay. See docs/OPERATIONS.md.
    """
    from brains.auth.copilot import (
        COPILOT_TOS_WARNING,
        CopilotAuthError,
        get_session,
        poll_device_flow,
        start_device_flow,
    )

    typer.secho(f"note: {COPILOT_TOS_WARNING}", fg=typer.colors.YELLOW, err=True)

    try:
        device = start_device_flow()
    except CopilotAuthError as exc:
        typer.secho(f"login failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"open {device.verification_uri} and enter code: {device.user_code}")
    typer.echo("waiting for authorization...")

    try:
        token = poll_device_flow(device.device_code, device.interval, device.expires_in)
    except CopilotAuthError as exc:
        typer.secho(f"login failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(f"authorized (oauth token length {len(token)})", fg=typer.colors.GREEN)
    try:
        session = get_session()
    except CopilotAuthError as exc:
        typer.secho(
            f"warning: session-token exchange failed: {exc}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    typer.echo(f"chat endpoint: {session.chat_base_url}/chat/completions")


@app.command("copilot-status")
def copilot_status_cli() -> None:
    """Show which OAuth source the github_copilot provider would use."""
    from brains.auth.copilot import auth_status

    _print_json(auth_status())


@app.command("copilot-logout")
def copilot_logout_cli() -> None:
    """Delete the cached Copilot OAuth + session tokens."""
    from brains.auth.copilot import clear_cached_credentials

    _print_json(clear_cached_credentials())


@app.command("classify")
def classify_cli(text: str):
    print(classify([{"role": "user", "content": text}]).model_dump_json(indent=2))


@app.command("plan")
def plan_cli(text: str):
    c = classify([{"role": "user", "content": text}])
    _print_json(plan(c))


@app.command("add-repo")
def add_repo(path: str):
    result = start_session(path, tool="brains-add-repo")
    end_session(result["session_id"], "registered workspace")
    _print_json({"added": path, "workspace": result["workspace"]})


@app.command("init")
def init_cli(
    path: str = typer.Argument(
        ".", help="Directory to register as a workspace (default: current dir)."
    ),
    slug: str | None = None,
    name: str | None = None,
):
    """Initialize the brains DB and register the current directory as a workspace.

    Idempotent: re-running just confirms the workspace is registered and the
    schema is up to date.
    """
    from brains.api.admin_key import admin_key_path, ensure_admin_key
    from brains.control.sessions import register_workspace
    from brains.storage.migrations import current_schema_versions, init_db

    init_db()
    workspace = register_workspace(path, slug=slug, name=name)
    _key, was_generated = ensure_admin_key(print_banner=True)
    # ensure_admin_key prints the one-time banner on stderr only when a key
    # was newly generated; the JSON below always records which path was used.
    _print_json(
        {
            "ok": True,
            "workspace": {
                "id": workspace.id,
                "slug": workspace.slug,
                "path": workspace.path,
                "name": workspace.name,
                "status": workspace.status,
            },
            "schema_versions": current_schema_versions(),
            "admin_key": {
                "source": "generated" if was_generated else "existing",
                "path": str(admin_key_path()),
            },
        }
    )


@app.command("setup")
def setup_cli(
    path: str = typer.Option(
        ".",
        "--path",
        help="Workspace path to register (default: current directory).",
    ),
    wire_tools: bool = typer.Option(
        True,
        "--wire/--no-wire",
        help="Register brains MCP into installed agentic tools (Copilot CLI, "
        "Claude Code, Codex). Default: yes.",
    ),
    transport: str = typer.Option(
        "streamable-http",
        "--transport",
        help="MCP transport for `wire`: 'streamable-http' (default), legacy 'sse', or 'stdio'.",
    ),
    port: int = typer.Option(
        9877,
        "--port",
        help="MCP port to wire (default 9877).",
    ),
    install_service: bool = typer.Option(
        False,
        "--service/--no-service",
        help="Also install brains serve-all as a user OS service that "
        "autostarts at login + restarts on failure. Default: no (off) — "
        "without it, setup just prints how to start the stack.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would happen — write nothing.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the full step-by-step report as JSON (for scripts / CI). "
        "Default is a human-readable progress summary.",
    ),
):
    """First-run bootstrap: init DB, register workspace, wire MCP, show
    optional-features status, and print the next command.

    Codex remote wiring intentionally fails closed unless
    ``BRAINS_MCP_BEARER_TOKEN`` already matches the effective Brains API
    credential in the environment that will launch Codex. Pre-provisioning
    matching ``BRAINS_API_KEY`` and ``BRAINS_MCP_BEARER_TOKEN`` permits one
    invocation. Otherwise initialize first, make the named bearer variable
    available securely, and rerun ``brains-ai wire``. Brains neither prints
    the generated key here nor changes its parent environment.

    Idempotent — safe to re-run; each step is its own subcommand
    (``init`` / ``wire`` / ``features --status`` / ``serve``) so you can
    redo any single piece without re-running the whole flow.

    Examples:

      brains-ai setup                       # init + wire + status, friendly text
      brains-ai setup --json                # same flow, machine-readable JSON
      brains-ai setup --no-wire             # init only (skip agentic-tool wiring)
      brains-ai setup --transport stdio     # use an explicit per-client subprocess
      brains-ai setup --dry-run             # preview every step, write nothing
    """
    from brains import wire as wire_mod
    from brains.api.admin_key import admin_key_path, ensure_admin_key
    from brains.control.sessions import register_workspace
    from brains.install import status_report
    from brains.mcp.transport import (
        MCP_MODE_SSE,
        MCP_MODE_STDIO,
        MCP_MODE_STREAMABLE_HTTP,
        mcp_http_url,
    )
    from brains.storage.migrations import current_schema_versions, init_db

    summary: dict[str, Any] = {"dry_run": dry_run, "steps": []}

    # --- Step 1: init -----------------------------------------------------
    if dry_run:
        summary["steps"].append(
            {
                "step": "init",
                "would_do": [
                    f"init_db() at {_canonical_db_url()}",
                    f"register_workspace(path={path!r})",
                    "ensure_admin_key()",
                ],
            }
        )
    else:
        init_db()
        workspace = register_workspace(path)
        _key, was_generated = ensure_admin_key(print_banner=False)
        summary["steps"].append(
            {
                "step": "init",
                "workspace": {
                    "id": workspace.id,
                    "slug": workspace.slug,
                    "path": workspace.path,
                },
                "schema_versions": current_schema_versions(),
                "admin_key": {
                    "source": "generated" if was_generated else "existing",
                    "path": str(admin_key_path()),
                },
            }
        )

    # --- Step 2: wire (optional) -----------------------------------------
    if wire_tools:
        if transport not in {MCP_MODE_STREAMABLE_HTTP, MCP_MODE_SSE, MCP_MODE_STDIO}:
            raise typer.BadParameter(
                "transport must be 'streamable-http', 'stdio', or legacy 'sse'"
            )
        wire_url = (
            f"http://127.0.0.1:{port}/sse" if transport == MCP_MODE_SSE else mcp_http_url(port=port)
        )
        ctx = wire_mod.WireContext(
            transport=transport,
            url=wire_url,
            python=sys.executable,
            db_url=_canonical_db_url(),
        )
        if transport != MCP_MODE_STDIO:
            ctx.api_key = _effective_wire_api_key(create=False)
        report = wire_mod.wire(Path.home(), ctx, dry_run=dry_run)
        summary["steps"].append({"step": "wire", "report": report})
    else:
        summary["steps"].append({"step": "wire", "skipped": True})

    # --- Step 3: features status -----------------------------------------
    try:
        summary["steps"].append({"step": "features_status", "report": status_report()})
    except Exception as exc:  # noqa: BLE001 — surface as a non-fatal note
        summary["steps"].append(
            {"step": "features_status", "error": f"{type(exc).__name__}: {exc}"}
        )

    # --- Step 3b: OS service (opt-in) ------------------------------------
    if install_service:
        from brains import service as service_mod

        if not service_mod.supported():
            summary["steps"].append(
                {
                    "step": "service",
                    "skipped": True,
                    "reason": f"unsupported platform {service_mod.current_platform()!r}",
                }
            )
        else:
            summary["steps"].append(
                {"step": "service", "report": service_mod.install(dry_run=dry_run)}
            )
    else:
        summary["steps"].append({"step": "service", "skipped": True})

    # --- Step 4: transport-specific next-command hint --------------------
    start_gateway = "brains-ai service status" if install_service else "brains-ai serve"
    summary["next"] = {
        "start_gateway": start_gateway,
        "mcp_transport": transport,
    }
    if not wire_tools:
        summary["next"].update(
            {
                "mcp_transport": None,
                "mcp_url": None,
                "tip": "MCP wiring was skipped; no MCP endpoint is claimed.",
            }
        )
    elif transport == MCP_MODE_STREAMABLE_HTTP:
        summary["next"].update(
            {
                "start_gateway": (
                    "brains-ai service status" if install_service else "brains-ai serve-all"
                ),
                "mcp_url": mcp_http_url(port=port),
                "mcp_auth_env": wire_mod.MCP_CLIENT_BEARER_ENV,
                "tip": (
                    "The hosted default uses authenticated Streamable HTTP. For Codex, "
                    f"{wire_mod.MCP_CLIENT_BEARER_ENV} must already match the effective "
                    "Brains API credential in the environment that launches Codex; "
                    "Brains cannot change its parent environment."
                ),
            }
        )
    elif transport == MCP_MODE_SSE:
        summary["next"].update(
            {
                "start_mcp": f"brains-ai mcp --mode sse --port {port}",
                "mcp_url": f"http://127.0.0.1:{port}/sse",
                "legacy": True,
                "tip": "SSE is an explicit legacy compatibility mode.",
            }
        )
    else:
        summary["next"].update(
            {
                "mcp_url": None,
                "tip": "stdio is client-spawned and has no HTTP endpoint or listener.",
            }
        )

    if json_out:
        _print_json(summary)
    else:
        _render_setup_text(summary, port=port)
    wire_step = next((step for step in summary["steps"] if step["step"] == "wire"), None)
    if wire_step and wire_step.get("report", {}).get("ok") is False:
        raise typer.Exit(code=1)


def _render_setup_text(summary: dict[str, Any], *, port: int) -> None:
    """Pretty-print the ``setup`` summary as a human-readable progress
    report. The same data is available via ``brains-ai setup --json``."""

    def line(s: str = "") -> None:
        typer.echo(s)

    def ok(label: str, value: str = "", note: str = "") -> None:
        marker = typer.style("OK ", fg=typer.colors.GREEN, bold=True)
        body = f"{marker}{label:<28}"
        if value:
            body += value
        if note:
            body += "  " + typer.style(note, fg=typer.colors.BRIGHT_BLACK)
        typer.echo(body)

    def warn(label: str) -> None:
        marker = typer.style("!  ", fg=typer.colors.YELLOW, bold=True)
        typer.echo(f"{marker}{label}")

    def header(n: int, total: int, title: str) -> None:
        line()
        prefix = typer.style(f"[{n}/{total}]", fg=typer.colors.CYAN, bold=True)
        line(f"{prefix} {title}")

    is_dry = summary.get("dry_run", False)
    title = "Brains setup — first-run wizard"
    if is_dry:
        title += "  (dry-run — no writes)"
    line()
    line(typer.style(title, bold=True))

    steps_by_kind = {s["step"]: s for s in summary.get("steps", [])}

    # ---------- Step 1 / 4: init ----------
    header(1, 4, "init")
    init = steps_by_kind.get("init", {})
    if is_dry:
        for action in init.get("would_do", []):
            ok("would do", action)
    else:
        ws = init.get("workspace", {})
        ak = init.get("admin_key", {})
        migrations = init.get("schema_versions", [])
        ok("Database initialized", f"{len(migrations)} migrations applied")
        ok(
            "Workspace registered",
            f"{ws.get('slug', '?')}  ({ws.get('path', '?')})",
        )
        source = ak.get("source", "?")
        ak_value = f"{source}  ({ak.get('path', '?')})"
        ok("Admin key", ak_value)

    # ---------- Step 2 / 4: wire ----------
    header(2, 4, "wire MCP into agentic tools")
    wire = steps_by_kind.get("wire", {})
    if wire.get("skipped"):
        warn("skipped (--no-wire)")
    else:
        report = wire.get("report", {}) or {}
        url = report.get("url", "?")
        line(f"    transport={report.get('transport', '?')}  url={url}")
        tools = report.get("tools", [])
        if not tools:
            warn("no agentic tools detected on this machine")
        for tool in tools:
            display = tool.get("display", tool.get("tool", "?"))
            mcp = tool.get("mcp", {}) or {}
            action = mcp.get("action", "?")
            backup = mcp.get("backup")
            note = f"backup: {backup}" if backup else ""
            ok(display, action, note)
            for warning in tool.get("warnings", []) or []:
                warn(f"  {display}: {warning}")

    # ---------- Step 3 / 4: features ----------
    header(3, 4, "optional features")
    feats = steps_by_kind.get("features_status", {})
    if "error" in feats:
        warn(feats["error"])
    else:
        report = feats.get("report", {}) or {}
        for feat in report.get("features", []):
            installed = feat.get("extra_installed", False)
            enabled = feat.get("config_enabled", False)
            if enabled:
                state = typer.style("enabled", fg=typer.colors.GREEN)
            elif installed:
                state = typer.style("installed, off", fg=typer.colors.YELLOW)
            else:
                state = typer.style("not installed", fg=typer.colors.BRIGHT_BLACK)
            slug = feat.get("feature", "?")
            label = feat.get("label", slug)
            hint = ""
            if not installed:
                hint = f"pipx install 'brains-ai[{slug}]'"
            elif not enabled:
                hint = f"brains-ai features enable {slug}"
            line(f"   {slug:<10} {state:<30} {label}")
            if hint:
                line(f"   {'':<10} {typer.style(hint, fg=typer.colors.BRIGHT_BLACK)}")

    # ---------- Step 3b: OS service (only when --service) ----------
    svc = steps_by_kind.get("service", {})
    if svc and not svc.get("skipped"):
        report = svc.get("report", {}) or {}
        line()
        action = report.get("action", "?")
        label = report.get("label", "service")
        if report.get("ok", action.startswith("would")):
            ok("Autostart service", f"{action}  ({label})")
        else:
            warn(f"service {action} reported: {report.get('detail', '?')}")

    # ---------- Step 4 / 4: next ----------
    header(4, 4, "next steps")
    nxt = summary.get("next", {})
    start = nxt.get("start_gateway", "brains-ai serve")
    mcp_transport = nxt.get("mcp_transport")
    line()
    line(f"   Start Brains:       {typer.style(start, bold=True)}")
    line()
    line("   Console:            http://127.0.0.1:8787/app")
    line("   Gateway:            http://127.0.0.1:8787")
    if mcp_transport == "streamable-http":
        line(f"   MCP (Streamable HTTP): {nxt.get('mcp_url')}")
        line(f"   Codex auth env:     {nxt.get('mcp_auth_env')}")
    elif mcp_transport == "sse":
        line(f"   MCP (legacy SSE):   {nxt.get('mcp_url')}")
        line(f"   Start legacy MCP:   {typer.style(nxt.get('start_mcp', ''), bold=True)}")
    elif mcp_transport == "stdio":
        line("   MCP:                 stdio (client-spawned; no HTTP endpoint or listener)")
    else:
        line("   MCP wiring:          skipped")
    line()
    line(
        "   Launch an LLM CLI through the gateway: "
        + typer.style("brains-ai run claude", bold=True)
        + " / "
        + typer.style("brains-ai run copilot", bold=True)
    )
    line()
    line(
        typer.style(
            "   Re-run with --json for the machine-readable report.",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )
    line()


@admin_key_app.command("show")
def admin_key_show_cli(
    reveal: bool = typer.Option(
        False,
        "--reveal",
        help="Print the actual key value (default: print only a short fingerprint).",
    ),
):
    """Show the persisted admin key (or its fingerprint).

    Treat the key as a secret — anyone with it controls the gateway.
    """
    import hashlib

    from brains.api.admin_key import (
        admin_key_path,
        ensure_admin_key,
        read_persisted_key,
    )

    key, was_generated = ensure_admin_key(print_banner=reveal)
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    payload: dict[str, object] = {
        "path": str(admin_key_path()),
        "source": (
            "BRAINS_API_KEY" if read_persisted_key() != key and not was_generated else "file"
        ),
        "fingerprint": fingerprint,
    }
    if reveal:
        payload["key"] = key
        payload["warning"] = "treat as secret; do not paste in logs or chats"
    _print_json(payload)


@admin_key_app.command("rotate")
def admin_key_rotate_cli(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
):
    """Generate a new admin key, overwrite the key file, invalidate existing cookies."""
    import hashlib

    from brains.api.admin_key import admin_key_path, rotate_admin_key

    if not yes:
        typer.confirm(
            "Rotate the admin key? Active /admin sessions will need to sign in again.",
            abort=True,
        )
    new_key = rotate_admin_key()
    fingerprint = hashlib.sha256(new_key.encode("utf-8")).hexdigest()[:16]
    _print_json(
        {
            "rotated": True,
            "path": str(admin_key_path()),
            "fingerprint": fingerprint,
            "note": "Use 'brains-ai admin-key show --reveal' to display the new key value.",
        }
    )


@admin_key_app.command("path")
def admin_key_path_cli():
    """Print the path of the persisted admin key file."""
    from brains.api.admin_key import admin_key_path

    print(admin_key_path())


@operator_app.command("add")
def operator_add_cli(
    slug: str = typer.Argument(
        ...,
        help="Operator slug (lowercase, [a-z0-9_-], 1-63 chars). "
        "Used in BRAINS_OPERATOR and on the dashboard header.",
    ),
    display_name: str | None = typer.Option(
        None,
        "--display-name",
        "-n",
        help="Human-readable name shown in dashboards. Defaults to the slug.",
    ),
    org: str | None = typer.Option(
        None,
        "--org",
        help="Org slug or id to grant the new operator membership of. Without it "
        "the operator authenticates but sees nothing (deny by default).",
    ),
    org_role: str = typer.Option(
        "member",
        "--org-role",
        help="Role to grant in --org: 'member', 'admin' or 'owner'.",
    ),
):
    """Mint a new operator and persist its API key.

    Prints the key value ONCE to stdout — copy it into the operator's
    client (Authorization: Bearer <key>) immediately. The key file is
    also written to ``~/.brains/operator-keys/<slug>.key`` for the
    gateway and Streamable HTTP MCP authentication to load on startup.
    """
    from brains.control.operators import (
        OperatorExistsError,
        OperatorSlugError,
        add_operator,
        ensure_admin_operator,
    )

    # Make sure admin row exists before we add siblings.
    ensure_admin_operator()
    try:
        record, key = add_operator(slug, display_name=display_name)
    except OperatorSlugError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    except OperatorExistsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    membership = None
    if org:
        from brains.control.orgs import add_member

        try:
            membership = add_member(org, record["slug"], role=org_role)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2)

    typer.echo(
        "==================================================================\n"
        f"  Operator '{record['slug']}' created.\n"
        "  API key (shown ONCE — copy it now):\n"
        f"    {key}\n"
        f"  Persisted to: ~/.brains/operator-keys/{record['slug']}.key\n"
        "==================================================================",
        err=True,
    )
    _print_json(
        {
            "ok": True,
            "operator": record,
            "key": key,
            "membership": membership,
            "warning": "key is shown once; treat as secret",
        }
    )


@operator_app.command("list")
def operator_list_cli():
    """List every operator. Fingerprints only — never raw keys."""
    from brains.control.operators import ensure_admin_operator, list_operators

    ensure_admin_operator()
    _print_json({"operators": list_operators()})


@operator_app.command("show")
def operator_show_cli():
    """Show the operator the current shell would resolve to.

    Follows the same priority as session attribution: BRAINS_OPERATOR
    env, then fingerprint match against BRAINS_API_KEY, then admin.
    """
    from brains.control.operators import resolve_current_operator

    _print_json({"operator": resolve_current_operator()})


@credentials_app.command("list")
def credentials_list_cli(
    kind: str | None = typer.Option(
        None, "--kind", help="Filter by credential kind: admin, operator or runtime."
    ),
    include_revoked: bool = typer.Option(
        False, "--include-revoked", help="Include credentials that have been revoked."
    ),
):
    """List accepted credentials. Fingerprints and bindings only — never secrets."""
    from brains.authz import credentials as creds

    creds.sync_local_credentials()
    _print_json({"credentials": creds.list_credentials(kind=kind, include_revoked=include_revoked)})


@credentials_app.command("revoke")
def credentials_revoke_cli(
    credential_id: str = typer.Argument(..., help="Public credential handle (cred id)."),
):
    """Revoke one credential immediately. Idempotent; takes effect on the next request."""
    from brains.authz import credentials as creds

    try:
        record = creds.revoke_credential(credential_id)
    except creds.CredentialError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    _print_json({"ok": True, "credential": record})


@credentials_app.command("revoke-machine")
def credentials_revoke_machine_cli(
    machine_id: str = typer.Argument(..., help="Machine whose Runtime credentials to revoke."),
):
    """Revoke every Runtime credential bound to a machine (disconnect a box)."""
    from brains.authz import credentials as creds

    _print_json({"ok": True, "revoked": creds.revoke_machine_credentials(machine_id)})


@credentials_app.command("doctor")
def credentials_doctor_cli():
    """Report credentials whose principal cannot be resolved unambiguously.

    Exits non-zero when anything is ambiguous, so an upgrade can be gated on a
    clean result. Nothing is deleted: a credential an operator still uses is
    reported, not removed.
    """
    from brains.authz import credentials as creds

    creds.sync_local_credentials()
    report = creds.diagnose()
    _print_json(report)
    if not report["ok"]:
        raise typer.Exit(code=1)


@workspace_app.command("invite")
def workspace_invite_cli(
    workspace: str = typer.Argument(
        ...,
        help="Workspace slug or absolute path. Looked up by slug first, then by path.",
    ),
    operator: str = typer.Argument(
        ...,
        help="Operator slug to invite. Must already exist (mint with 'brains-ai operator add').",
    ),
    role: str = typer.Option(
        "member",
        "--role",
        "-r",
        help="Membership role ('member' or 'owner'). Informational today; "
        "the visibility check is binary.",
    ),
):
    """Grant ``operator`` access to ``workspace``.

    Idempotent — re-running with the same (workspace, operator) just
    confirms or updates the role. Inviting ``admin`` is a no-op since
    admin has implicit membership on every workspace.
    """
    from brains.control.memberships import (
        MembershipRoleError,
        OperatorLookupError,
        WorkspaceLookupError,
        add_membership,
    )
    from brains.control.operators import OperatorSlugError, ensure_admin_operator

    ensure_admin_operator()
    try:
        record = add_membership(workspace, operator, role=role)
    except (
        MembershipRoleError,
        OperatorLookupError,
        WorkspaceLookupError,
        OperatorSlugError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    _print_json({"ok": True, "membership": record})


@workspace_app.command("uninvite")
def workspace_uninvite_cli(
    workspace: str = typer.Argument(
        ...,
        help="Workspace slug or absolute path.",
    ),
    operator: str = typer.Argument(
        ...,
        help="Operator slug to revoke.",
    ),
):
    """Revoke ``operator``'s access to ``workspace``.

    Exits non-zero if no membership row exists.
    """
    from brains.control.memberships import (
        MembershipNotFoundError,
        OperatorLookupError,
        WorkspaceLookupError,
        remove_membership,
    )
    from brains.control.operators import OperatorSlugError, ensure_admin_operator

    ensure_admin_operator()
    try:
        record = remove_membership(workspace, operator)
    except MembershipNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except (OperatorLookupError, WorkspaceLookupError, OperatorSlugError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    _print_json({"ok": True, "removed": record})


@workspace_app.command("members")
def workspace_members_cli(
    workspace: str = typer.Argument(
        ...,
        help="Workspace slug or absolute path.",
    ),
):
    """List every operator with explicit access to ``workspace``.

    Admin always has implicit access and is NOT shown in this list —
    it isn't stored in the membership table. Treat admin as an
    implicit member of every workspace.
    """
    from brains.control.memberships import (
        WorkspaceLookupError,
        list_memberships,
    )
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    try:
        rows = list_memberships(workspace=workspace)
    except WorkspaceLookupError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    _print_json({"workspace": workspace, "members": rows})


@workspace_app.command("visibility")
def workspace_visibility_cli(
    workspace: str = typer.Argument(
        ...,
        help="Workspace slug or absolute path.",
    ),
    visibility: str = typer.Argument(
        ...,
        help="One of 'shared' (every operator can see it; the default) "
        "or 'private' (only invited operators + admin can see it).",
    ),
):
    """Flip a workspace between ``shared`` and ``private``."""
    from brains.control.memberships import (
        WorkspaceLookupError,
        WorkspaceVisibilityError,
        set_workspace_visibility,
    )
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    try:
        result = set_workspace_visibility(workspace, visibility)
    except WorkspaceVisibilityError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    except WorkspaceLookupError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    _print_json({"ok": True, **result})


@workspace_app.command("show")
def workspace_show_cli(
    workspace: str = typer.Argument(
        ...,
        help="Workspace slug or absolute path.",
    ),
):
    """Show a workspace's current visibility and members."""
    from brains.control.memberships import (
        WorkspaceLookupError,
        list_memberships,
    )
    from brains.control.operators import ensure_admin_operator
    from brains.storage import db as _db_module
    from brains.storage.migrations import init_db
    from brains.storage.models import Workspace as _Workspace

    ensure_admin_operator()
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(_Workspace).filter(_Workspace.slug == workspace).one_or_none()
        if row is None:
            row = session.query(_Workspace).filter(_Workspace.path == workspace).one_or_none()
        if row is None:
            typer.echo(
                f"error: workspace {workspace!r} not found (looked up by slug and by path)",
                err=True,
            )
            raise typer.Exit(code=2)
        payload: dict[str, Any] = {
            "workspace": {
                "id": row.id,
                "slug": row.slug,
                "path": row.path,
                "visibility": row.visibility,
            },
        }
    try:
        payload["members"] = list_memberships(workspace=workspace)
    except WorkspaceLookupError:
        payload["members"] = []
    _print_json(payload)


@app.command("upgrade")
def upgrade_cli(
    no_init: bool = typer.Option(
        False,
        "--no-init",
        help="Skip running 'brains-ai init' after the upgrade.",
    ),
    git_remote: str = typer.Option(
        "origin",
        "--git-remote",
        help="Git remote to pull from.",
    ),
    git_branch: str | None = typer.Option(
        None,
        "--git-branch",
        help="Git branch to fast-forward to. Defaults to the currently checked-out branch.",
    ),
):
    """Upgrade an editable git-checkout install of brains.

    Runs ``git pull --ff-only`` then ``pip install -e . --upgrade`` in the
    repo root, then applies pending DB migrations. Fails loud with a manual
    upgrade hint if the package wasn't installed from a git checkout.
    """
    import subprocess
    import sys as _sys
    from pathlib import Path

    import brains as _brains

    pkg_dir = Path(_brains.__file__).resolve().parent
    # src/brains -> src -> repo root
    repo_root = pkg_dir.parent.parent
    if not (repo_root / ".git").exists():
        typer.echo(
            "brains is not installed from a git checkout.\n"
            f"  Detected install root: {repo_root}\n"
            "  To upgrade, reinstall from your source distribution:\n"
            "    pip install --upgrade brains-ai\n"
            "  or pull + reinstall manually from your fork.",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"[upgrade] repo: {repo_root}")
    pull_cmd = ["git", "pull", "--ff-only", git_remote]
    if git_branch:
        pull_cmd.append(git_branch)
    typer.echo(f"[upgrade] $ {' '.join(pull_cmd)}")
    subprocess.run(pull_cmd, cwd=repo_root, check=True)

    pip_cmd = [_sys.executable, "-m", "pip", "install", "-e", ".", "--upgrade"]
    typer.echo(f"[upgrade] $ {' '.join(pip_cmd)}")
    subprocess.run(pip_cmd, cwd=repo_root, check=True)

    if not no_init:
        from brains.storage.migrations import init_db

        typer.echo("[upgrade] applying pending DB migrations...")
        init_db()

    from brains import __version__ as new_version

    typer.echo(f"[upgrade] done — brains {new_version}")


@app.command("features")
def features_cli(
    features: str | None = typer.Option(
        None,
        "--features",
        help="Comma-separated full feature set (replaces current selection). "
        "Mutually exclusive with --enable / --disable.",
    ),
    enable: list[str] = typer.Option(
        [],
        "--enable",
        help="Add a feature to the enabled set. Repeatable.",
    ),
    disable: list[str] = typer.Option(
        [],
        "--disable",
        help="Remove a feature from the enabled set. Repeatable.",
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help="Show every feature's install + config state and exit. No changes.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Non-interactive: apply the computed plan without prompting.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the plan and exit. Never writes overlay or runs pip.",
    ),
    run_pip: bool = typer.Option(
        False,
        "--run-pip",
        help="Actually run `pip install brains-ai[...]` for missing extras. "
        "Off by default — the safer behaviour is to print the command.",
    ),
):
    """Interactive subsystem wizard: pick optional features, write the overlay.

    Examples:

      brains-ai features                                # interactive picker
      brains-ai features --status                       # report current state
      brains-ai features --features telegram,slack -y   # set exact set, no prompt
      brains-ai features --enable telegram --yes        # add one
      brains-ai features --disable whatsapp --yes       # turn one off
      brains-ai features --features otel --run-pip -y   # also run pip install
    """
    _run_features_command(
        features=features,
        enable=enable,
        disable=disable,
        status=status,
        yes=yes,
        dry_run=dry_run,
        run_pip=run_pip,
    )


def _run_features_command(
    *,
    features: str | None,
    enable: list[str],
    disable: list[str],
    status: bool,
    yes: bool,
    dry_run: bool,
    run_pip: bool,
) -> None:
    from brains.install import (
        SPECS,
        VALID_FEATURES,
        apply_plan,
        format_plan,
        plan_changes,
        status_report,
    )

    if status:
        _print_json(status_report())
        return

    if features is not None and (enable or disable):
        raise typer.BadParameter("--features is mutually exclusive with --enable / --disable")

    feature_list: list[str] | None = None
    if features is not None:
        feature_list = [piece.strip() for piece in features.split(",") if piece.strip()]
        unknown = sorted(set(feature_list) - set(VALID_FEATURES))
        if unknown:
            raise typer.BadParameter(f"Unknown features: {unknown}. Valid: {list(VALID_FEATURES)}")

    interactive = features is None and not enable and not disable and not yes
    if interactive:
        report = status_report()
        typer.echo("Current subsystem state:")
        chosen: list[str] = []
        for row in report["features"]:
            label = f"  {row['feature']:<10} {row['label']}"
            mark = "[on]" if row["config_enabled"] else "[--]"
            install_mark = "(installed)" if row["extra_installed"] else "(extra missing)"
            typer.echo(f"{mark} {label}  {install_mark}")
        typer.echo("")
        for feature in VALID_FEATURES:
            spec = SPECS[feature]
            default = next(
                (r["config_enabled"] for r in report["features"] if r["feature"] == feature),
                False,
            )
            keep = typer.confirm(f"Enable {getattr(spec, 'label', feature)}?", default=default)
            if keep:
                chosen.append(feature)
        feature_list = chosen

    plan = plan_changes(
        enable=enable or None,
        disable=disable or None,
        features=feature_list,
    )
    typer.echo(format_plan(plan))

    if dry_run:
        _print_json({"plan": _plan_payload(plan), "applied": False})
        return

    if not plan.has_changes:
        _print_json({"plan": _plan_payload(plan), "applied": False})
        return

    if not yes and not interactive:
        if not typer.confirm("Apply this plan?", default=False):
            _print_json({"plan": _plan_payload(plan), "applied": False})
            return
    elif interactive and not typer.confirm("Write these changes?", default=True):
        _print_json({"plan": _plan_payload(plan), "applied": False})
        return

    pip_consent = run_pip
    if plan.pip_command is not None and not run_pip and not yes:
        pip_consent = typer.confirm("Run pip install for missing extras?", default=False)

    result = apply_plan(plan, run_pip=pip_consent)
    if not pip_consent and plan.pip_command is not None:
        typer.echo("Skipped pip install. Run this when ready:\n  $ " + " ".join(plan.pip_command))
    _print_json({"plan": _plan_payload(plan), "applied": True, "result": result})


def _plan_payload(plan) -> dict:
    return {
        "features_to_enable": plan.features_to_enable,
        "features_to_disable": plan.features_to_disable,
        "extras_to_install": plan.extras_to_install,
        "overlay_updates": plan.overlay_updates,
        "skipped_no_change": plan.skipped_no_change,
        "pip_command": plan.pip_command,
    }


@app.command("workspace-import")
def workspace_import_cli(file: str):
    """Bulk-register workspaces from a JSON file.

    File format: a JSON array of objects with at least ``path`` and
    optionally ``slug`` / ``name``. Unknown keys are ignored. The
    command is idempotent — workspaces with a matching path are
    returned untouched.
    """
    from pathlib import Path

    from brains.control.sessions import register_workspace
    from brains.storage.migrations import init_db

    init_db()
    raw = Path(file).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise typer.BadParameter("workspace-import file must be a JSON array")
    out: list[dict] = []
    for entry in payload:
        if not isinstance(entry, dict) or "path" not in entry:
            raise typer.BadParameter("every entry must be an object with a 'path' field")
        workspace = register_workspace(
            entry["path"],
            slug=entry.get("slug"),
            name=entry.get("name"),
        )
        out.append(
            {
                "id": workspace.id,
                "slug": workspace.slug,
                "path": workspace.path,
                "name": workspace.name,
            }
        )
    _print_json({"imported": len(out), "workspaces": out})


@app.command("index-repo")
def index_repo_cli(path: str):
    print(len(index_repo(path)))


@app.command("repo-index")
def repo_index_persist_cli(path: str = "."):
    """Walk the workspace, upsert artifacts with content-hash dedup."""
    _print_json(index_repo_persisted(path))


@app.command("repo-search")
def repo_search_persist_cli(q: str, path: str = ".", limit: int = 50):
    """Substring search across artifacts persisted by ``repo-index``."""
    _print_json(search_repo_persisted(path, q, limit=limit))


@app.command("graph-export")
def graph_export_cli(workspace_path: str = ".", out_dir: str = "."):
    """Export the visible code graph as standalone SVG + HTML files."""
    _require_experimental_cli("code graph export")
    _print_json(graph_export(workspace_path, out_dir))


@app.command("graph-build")
def graph_build_cli(workspace_path: str = ".", max_files: int = 2000):
    """Build (or rebuild) the code graph for a workspace. Usually unnecessary —
    graph queries auto-build on first use."""
    _require_experimental_cli("code graph build")
    from brains.context.code_graph import build_code_graph

    _print_json(build_code_graph(workspace_path, max_files=max_files))


@app.command("graph-query")
def graph_query_cli(
    question: str, workspace_path: str = ".", depth: int = 2, token_budget: int = 2000
):
    """Natural-language code-graph query (callers, impact, structure). Auto-builds
    the graph on first use."""
    _require_experimental_cli("code graph query")
    from brains.context.code_graph import graph_query

    print(graph_query(workspace_path, question, depth=depth, token_budget=token_budget))


@app.command("graph-neighbors")
def graph_neighbors_cli(
    node_query: str,
    workspace_path: str = ".",
    relation: str | None = None,
    limit: int = 50,
):
    """Neighbours (callers/callees/imports/contains) of a graph node. Auto-builds."""
    _require_experimental_cli("code graph neighbors")
    from brains.context.code_graph import graph_neighbors

    _print_json(graph_neighbors(workspace_path, node_query, relation=relation, limit=limit))


@app.command("graph-path")
def graph_path_cli(src_query: str, dst_query: str, workspace_path: str = ".", max_depth: int = 6):
    """Shortest relationship path between two graph nodes. Auto-builds."""
    _require_experimental_cli("code graph path")
    from brains.context.code_graph import graph_path

    _print_json(graph_path(workspace_path, src_query, dst_query, max_depth=max_depth))


@app.command("graph-subsystems")
def graph_subsystems_cli(workspace_path: str = "."):
    """List detected subsystems (graph communities). Auto-builds."""
    _require_experimental_cli("code graph subsystems")
    from brains.context.code_graph import list_subsystems

    _print_json(list_subsystems(workspace_path))


@app.command("docs-index")
def docs_index_cli(workspace: str = "."):
    _require_experimental_cli("docs indexing for semantic retrieval")
    result = index_docs(workspace)
    refresh_views(workspace)
    _print_json({"workspace": result["workspace"], "count": result["count"]})


@app.command("search-repo")
def search_repo_cli(q: str, path: str = "."):
    _print_json(search_docs(path, q) or search_repo(path, q))


@app.command("embed-repo")
def embed_repo_cli(path: str = ".", model: str | None = None):
    """Chunk + embed the workspace repo for semantic search.

    Requires an embedding model — set `embed_model` (e.g. nomic-embed-text)
    in config/overlay, or pass --model. Re-running is cheap (content-hash
    deduped); only changed files are re-embedded.

    Experimental: requires BRAINS_MCP_EXPERIMENTAL=1.
    """
    _require_experimental_cli("repo embedding")
    from brains.context.semantic import embed_repo

    _print_json(embed_repo(path, model=model))


@app.command("search-semantic")
def search_semantic_cli(
    q: str,
    path: str = ".",
    limit: int = 10,
    model: str | None = None,
    exclude: str | None = None,
    include: str | None = None,
):
    """Semantic (embedding cosine) repo search. Run `brains-ai embed-repo` first.

    --exclude/--include take comma-separated path substrings/globs, e.g.
    --exclude "/docs/,/tests/,test_" to surface implementation over docs/tests.

    Experimental: requires BRAINS_MCP_EXPERIMENTAL=1.
    """
    _require_experimental_cli("semantic search")
    from brains.context.semantic import semantic_search_with_status

    inc = [s.strip() for s in include.split(",") if s.strip()] if include else None
    exc = [s.strip() for s in exclude.split(",") if s.strip()] if exclude else None
    _print_json(
        semantic_search_with_status(path, q, limit=limit, model=model, include=inc, exclude=exc)
    )


@app.command("orient")
def orient_cli(
    query: str | None = None,
    path: str = ".",
    limit: int = 8,
    include_docs: bool = False,
    include_tests: bool = False,
    exclude: str | None = None,
):
    """Print a ready-to-INJECT markdown orientation block: brains' ranked
    implementation files for a task, so a (cheap) model can navigate a large repo
    WITHOUT grepping. Prepend the output to the agent's prompt at session start.

    Implementation-focused by default (docs + tests excluded); --include-docs /
    --include-tests widen. If --query is omitted, the active handoff for this
    workspace seeds it (so a launcher can call `brains-ai orient --path <repo>`
    with zero task args). Run `brains-ai embed-repo` first (or rely on auto-embed).
    """
    from brains.context.semantic import build_orientation_block

    seed = query
    if not seed:
        try:
            from brains.control.sessions import register_workspace
            from brains.storage.db import SessionLocal
            from brains.storage.migrations import init_db
            from brains.storage.models import Handoff

            init_db()
            with SessionLocal() as session:
                ws = register_workspace(path)
                active = (
                    session.query(Handoff)
                    .filter(Handoff.workspace_id == ws.id, Handoff.status == "active")
                    .order_by(Handoff.set_at.desc(), Handoff.id.desc())
                    .first()
                )
                if active:
                    seed = f"{active.title}. {active.body or ''}".strip()
        except Exception:
            seed = None
    if not seed:
        print(
            "<!-- brains orientation: no --query given and no active handoff to "
            "seed from; pass a task description. -->"
        )
        return
    exc = [s.strip() for s in exclude.split(",") if s.strip()] if exclude else None
    print(
        build_orientation_block(
            path,
            seed,
            limit=limit,
            include_docs=include_docs,
            include_tests=include_tests,
            exclude=exc,
        )
    )


@app.command("live-agents")
def live_agents_cli(ttl_seconds: int = typer.Option(900, "--ttl-seconds")):
    """List every live agent session on this brain, across all workspaces."""
    from brains.control.topics import live_agent_sessions

    _print_json(live_agent_sessions(ttl_seconds=ttl_seconds))


@app.command("topic-post")
def topic_post_cli(
    topic: str,
    subject: str,
    body: str = typer.Option("", "--body"),
    workspace: str = typer.Option(".", "--workspace"),
    session: str | None = typer.Option(None, "--session"),
    required_tool: str | None = typer.Option(None, "--required-tool"),
    reply_to: int | None = typer.Option(None, "--reply-to"),
    no_blast: bool = typer.Option(False, "--no-blast"),
):
    """Post to a topic board; wakes interested live subscribers."""
    from brains.control.topics import post_topic

    _print_json(
        post_topic(
            topic,
            subject,
            body,
            from_session_id=session,
            workspace_path=workspace,
            required_tool=required_tool,
            reply_to=reply_to,
            blast=not no_blast,
        )
    )


@app.command("topic-read")
def topic_read_cli(
    topic: str | None = typer.Argument(None),
    limit: int = typer.Option(50, "--limit"),
    reply_to: int | None = typer.Option(None, "--reply-to"),
    session: str | None = typer.Option(None, "--session"),
    after_post_id: int | None = typer.Option(None, "--after-post-id"),
):
    """Read a board; a Session-scoped read advances its subscription cursor."""
    from brains.control.topics import read_topic

    _print_json(
        read_topic(
            topic,
            limit=limit,
            reply_to=reply_to,
            session_id=session,
            after_post_id=after_post_id,
        )
    )


@app.command("topic-list")
def topic_list_cli(limit: int = typer.Option(100, "--limit")):
    """List topics with post counts and latest activity."""
    from brains.control.topics import list_topics

    _print_json(list_topics(limit=limit))


@app.command("topic-subscribe")
def topic_subscribe_cli(
    topic: str,
    session: str = typer.Option(..., "--session"),
    include_existing: bool = typer.Option(False, "--include-existing"),
):
    """Subscribe a Session to topic wakeups."""
    from brains.control.topics import subscribe_topic

    _print_json(subscribe_topic(topic, session, include_existing=include_existing))


@app.command("topic-unsubscribe")
def topic_unsubscribe_cli(topic: str, session: str = typer.Option(..., "--session")):
    """Remove a Session's topic subscription."""
    from brains.control.topics import unsubscribe_topic

    _print_json(unsubscribe_topic(topic, session))


@app.command("topic-subscriptions")
def topic_subscriptions_cli(session: str = typer.Option(..., "--session")):
    """List one Session's topic cursors and pending counts."""
    from brains.control.topics import list_topic_subscriptions

    _print_json(list_topic_subscriptions(session))


@app.command("mail-send")
def mail_send_cli(
    to: str = typer.Option(...),
    subject: str = typer.Option(...),
    body: str = typer.Option("", "--body"),
    session: str | None = typer.Option(None, "--session"),
):
    """Send one outbound email via configured SMTP (SES = config only)."""
    from brains.control.mailer import MailerError, send_email

    try:
        _print_json(send_email(to, subject, body, session_id=session))
    except MailerError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None


@app.command("mail-status")
def mail_status_cli():
    """Redacted mailer configuration snapshot."""
    from brains.control.mailer import mailer_status

    _print_json(mailer_status())


@app.command("inbox-wait")
def inbox_wait_cli(
    session: str = typer.Option(...),
    timeout_ms: int = typer.Option(25000, "--timeout-ms"),
    after_message_id: int | None = typer.Option(None, "--after-message-id"),
):
    """Block until mail, a subscribed topic, or a peer request arrives."""
    from brains.control.mailbox import inbox_wait

    _print_json(
        inbox_wait(
            session,
            timeout_ms=timeout_ms,
            after_message_id=after_message_id,
        )
    )


@app.command("help-file")
def help_file_cli(
    subject: str = typer.Option(..., "--subject"),
    question: str = typer.Option(..., "--question"),
    from_session: str | None = typer.Option(None, "--from-session"),
    to_workspace: str | None = typer.Option(None, "--to-workspace"),
    to_session: str | None = typer.Option(None, "--to-session"),
    context: str = typer.Option("", "--context"),
    timeout_ms: int = typer.Option(30000, "--timeout-ms"),
    required_tool: str | None = typer.Option(None, "--required-tool"),
):
    """File durable help for an existing peer and return immediately."""
    from brains.control.help import file_help_request

    _print_json(
        file_help_request(
            subject,
            question,
            from_session_id=from_session,
            to_workspace=to_workspace,
            to_session_id=to_session,
            context=context,
            timeout_ms=timeout_ms,
            required_tool=required_tool,
            execution_mode="existing",
        )
    )


@app.command("help-get")
def help_get_cli(
    code: str,
    session: str | None = typer.Option(None, "--session"),
):
    """Read one peer-help request without blocking."""
    from brains.control.help import get_help_request

    _print_json(get_help_request(code, session_id=session))


@app.command("help-wait")
def help_wait_cli(
    code: str,
    session: str | None = typer.Option(None, "--session"),
    timeout_ms: int = typer.Option(30000, "--timeout-ms"),
):
    """Wait briefly for one request without expiring it on timeout."""
    from brains.control.help import wait_help_request

    _print_json(wait_help_request(code, session_id=session, timeout_ms=timeout_ms))


@app.command("help-claim")
def help_claim_cli(
    session: str = typer.Option(..., "--session"),
    workspace: str | None = typer.Option(None, "--workspace"),
    timeout_ms: int = typer.Option(30000, "--timeout-ms"),
):
    """Wait for and claim peer help routed to this Session or Workspace."""
    from brains.control.help import wait_for_request

    _print_json(
        wait_for_request(
            session_id=session,
            workspace_slug=workspace,
            timeout_ms=timeout_ms,
        )
    )


@app.command("help-answer")
def help_answer_cli(
    code: str,
    answer: str = typer.Option(..., "--answer"),
    evidence: str = typer.Option(..., "--evidence"),
    session: str = typer.Option(..., "--session"),
):
    """Answer help claimed by this Session with required evidence."""
    from brains.control.help import answer_request

    _print_json(answer_request(code, answer, evidence, session_id=session))


@app.command("help-cancel")
def help_cancel_cli(
    code: str,
    session: str = typer.Option(..., "--session"),
):
    """Cancel help as the Session that filed it."""
    from brains.control.help import cancel_help_request

    _print_json(cancel_help_request(code, session_id=session))


@app.command("help-release")
def help_release_cli(
    code: str,
    session: str = typer.Option(..., "--session"),
):
    """Release claimed help back to the open queue."""
    from brains.control.help import release_help_request

    _print_json(release_help_request(code, session_id=session))


@app.command("help-list")
def help_list_cli(
    to_workspace: str | None = typer.Option(None, "--to-workspace"),
    to_session: str | None = typer.Option(None, "--to-session"),
    include_answered: bool = typer.Option(False, "--include-answered"),
    limit: int = typer.Option(50, "--limit"),
):
    """List visible peer-help requests."""
    from brains.control.help import list_open_help_requests

    _print_json(
        list_open_help_requests(
            to_workspace=to_workspace,
            to_session_id=to_session,
            include_answered=include_answered,
            limit=limit,
        )
    )


def _feedback_metadata(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("metadata must be a JSON object") from exc
    if not isinstance(value, dict):
        raise typer.BadParameter("metadata must be a JSON object")
    return value


@app.command("feedback-report")
def feedback_report_cli(
    category: str = typer.Option(..., "--category"),
    severity: str = typer.Option(..., "--severity"),
    summary: str = typer.Option(..., "--summary"),
    workspace: str = typer.Option(".", "--workspace"),
    evidence: str = typer.Option("", "--evidence"),
    reproduction: str = typer.Option("", "--reproduction"),
    affected_version: str | None = typer.Option(None, "--affected-version"),
    surface: str | None = typer.Option(None, "--surface"),
    session: str = typer.Option(..., "--session"),
    metadata: str = typer.Option("", "--metadata"),
):
    """File a redacted Workspace-scoped agent-experience report."""
    from brains.control.feedback import file_feedback

    _print_json(
        file_feedback(
            workspace,
            category,
            severity,
            summary,
            evidence=evidence,
            reproduction=reproduction,
            affected_version=affected_version,
            surface=surface,
            reporter_session_id=session,
            metadata=_feedback_metadata(metadata),
        )
    )


@app.command("feedback-enrich")
def feedback_enrich_cli(
    code: str,
    session: str = typer.Option(..., "--session"),
    kind: str = typer.Option("enrichment", "--kind"),
    note: str = typer.Option("", "--note"),
    evidence: str = typer.Option("", "--evidence"),
    reproduction: str = typer.Option("", "--reproduction"),
    metadata: str = typer.Option("", "--metadata"),
):
    """Add redacted evidence from a live Session in the report's Workspace."""
    from brains.control.feedback import enrich_feedback

    _print_json(
        enrich_feedback(
            code,
            reporter_session_id=session,
            kind=kind,
            note=note,
            evidence=evidence,
            reproduction=reproduction,
            metadata=_feedback_metadata(metadata),
        )
    )


@app.command("feedback-get")
def feedback_get_cli(code: str):
    """Read one visible feedback report."""
    from brains.control.feedback import get_feedback

    _print_json(get_feedback(code))


@app.command("feedback-list")
def feedback_list_cli(
    workspace: str | None = typer.Option(None, "--workspace"),
    status: str | None = typer.Option(None, "--status"),
    category: str | None = typer.Option(None, "--category"),
    limit: int = typer.Option(100, "--limit"),
):
    """List visible feedback reports."""
    from brains.control.feedback import list_feedback

    _print_json(list_feedback(workspace, status=status, category=category, limit=limit))


@app.command("feedback-triage")
def feedback_triage_cli(
    code: str,
    status: str = typer.Option(..., "--status"),
    note: str = typer.Option("", "--note"),
    operator: str | None = typer.Option(None, "--operator"),
):
    """Human-only feedback lifecycle transition."""
    from brains.authz.resolver import resolve_local_principal
    from brains.control.feedback import triage_feedback

    _print_json(
        triage_feedback(
            code,
            status,
            note=note,
            principal=resolve_local_principal(operator=operator),
        )
    )


@app.command("feedback-promote")
def feedback_promote_cli(
    code: str,
    target: str = typer.Option(..., "--target"),
    backlog_ref: str | None = typer.Option(None, "--backlog-ref"),
    operator: str | None = typer.Option(None, "--operator"),
):
    """Human-only exactly-once promotion to Task, knowledge, or backlog reference."""
    from brains.authz.resolver import resolve_local_principal
    from brains.control.feedback import promote_feedback

    _print_json(
        promote_feedback(
            code,
            target,
            backlog_ref=backlog_ref,
            principal=resolve_local_principal(operator=operator),
        )
    )


@app.command("check-source")
def check_source_cli(source: str):
    _print_json(check_source(source))


@app.command()
def models():
    print("brains-auto")


@app.command()
def traces(limit: int = 20):
    rows = list_traces(limit=limit)
    _print_json([{"id": r.id, "route": r.route} for r in rows])


@app.command("state")
def state_cli(
    workspace: str | None = None,
    session: str | None = None,
    limit: int = 50,
):
    _print_json(get_state(workspace_path=workspace, session_id=session, limit=limit))


@app.command("session-start")
def session_start_cli(
    workspace: str = ".",
    tool: str = "codex",
    pid: int | None = typer.Option(
        None,
        "--pid",
        min=1,
        help="PID of the durable agent process that owns this Session. Omit when unknown.",
    ),
    predecessor_session: str | None = typer.Option(None, "--predecessor-session"),
    new: bool = typer.Option(
        False,
        "--new",
        help="Create a distinct handle instead of reusing this workspace/tool/operator Session.",
    ),
    native_tool_session_id: str | None = typer.Option(None, "--native-tool-session-id"),
    mailbox_binding_file: Path | None = typer.Option(
        None,
        "--mailbox-binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Adapter-owned file containing the mailbox reattachment secret.",
    ),
    mailbox_notification_mode: str | None = typer.Option(None, "--mailbox-notification-mode"),
):
    mailbox_binding_secret = (
        read_mailbox_binding_file(mailbox_binding_file)
        if mailbox_binding_file is not None
        else None
    )
    _print_json(
        start_session(
            workspace,
            tool=tool,
            pid=pid,
            predecessor_session_id=predecessor_session,
            reuse_existing=not new,
            auto_link_predecessor=True,
            native_tool_session_id=native_tool_session_id,
            mailbox_binding_secret=mailbox_binding_secret,
            mailbox_notification_mode=mailbox_notification_mode,
        )
    )


@app.command("session-heartbeat")
def session_heartbeat_cli(
    session: str = typer.Option(...),
    tool: str | None = typer.Option(None, "--tool"),
    native_tool_session_id: str | None = typer.Option(None, "--native-tool-session-id"),
    mailbox_binding_file: Path | None = typer.Option(
        None,
        "--mailbox-binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    mailbox_notification_mode: str | None = typer.Option(None, "--mailbox-notification-mode"),
):
    from brains.control.sessions import heartbeat_session

    mailbox_binding_secret = (
        read_mailbox_binding_file(mailbox_binding_file)
        if mailbox_binding_file is not None
        else None
    )
    _print_json(
        heartbeat_session(
            session,
            tool=tool,
            native_tool_session_id=native_tool_session_id,
            mailbox_binding_secret=mailbox_binding_secret,
            mailbox_notification_mode=mailbox_notification_mode,
        )
    )


@mailbox_app.command("register")
def mailbox_register_cli(
    workspace: str = typer.Option(".", "--workspace"),
    tool: str = typer.Option(..., "--tool"),
    native_tool_session_id: str = typer.Option(..., "--native-tool-session-id"),
    session: str = typer.Option(..., "--session"),
    binding_file: Path = typer.Option(
        ...,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Adapter-owned file containing the mailbox reattachment secret.",
    ),
    notification_mode: str | None = typer.Option(None, "--notification-mode"),
):
    binding_secret = read_mailbox_binding_file(binding_file)
    _print_json(
        register_agent_mailbox(
            workspace,
            tool,
            native_tool_session_id,
            session,
            binding_secret,
            notification_mode=notification_mode or "pull",
        )
    )


@mailbox_app.command("native-id")
def mailbox_native_id_cli(
    adapter: str = typer.Option(..., "--adapter"),
    copilot_session_id: str | None = typer.Option(None, "--copilot-session-id"),
    claude_session_id: str | None = typer.Option(None, "--claude-session-id"),
    codex_thread_id: str | None = typer.Option(None, "--codex-thread-id"),
    codex_session_id: str | None = typer.Option(None, "--codex-session-id"),
    opencode_session_id: str | None = typer.Option(None, "--opencode-session-id"),
):
    _print_json(
        extract_native_tool_session_id(
            adapter,
            {
                "copilot_session_id": copilot_session_id,
                "claude_session_id": claude_session_id,
                "codex_thread_id": codex_thread_id,
                "codex_session_id": codex_session_id,
                "opencode_session_id": opencode_session_id,
            },
        )
    )


def _managed_mailbox_action(
    action: str, workspace: str, adapter: str, native_id: str, session: str
) -> None:
    controls = {
        "create": create_managed_agent_mailbox,
        "rotate": rotate_managed_agent_mailbox_binding,
        "recover": recover_managed_agent_mailbox_binding,
        "revoke": revoke_managed_agent_mailbox_binding,
    }
    _print_json(controls[action](workspace, adapter, native_id, session))


@mailbox_app.command("managed-create")
def mailbox_managed_create_cli(
    workspace: str = typer.Option(".", "--workspace"),
    adapter: str = typer.Option(..., "--adapter"),
    native_id: str = typer.Option(..., "--native-tool-session-id"),
    session: str = typer.Option(..., "--session"),
):
    _managed_mailbox_action("create", workspace, adapter, native_id, session)


@mailbox_app.command("managed-rotate")
def mailbox_managed_rotate_cli(
    workspace: str = typer.Option(".", "--workspace"),
    adapter: str = typer.Option(..., "--adapter"),
    native_id: str = typer.Option(..., "--native-tool-session-id"),
    session: str = typer.Option(..., "--session"),
):
    _managed_mailbox_action("rotate", workspace, adapter, native_id, session)


@mailbox_app.command("managed-recover")
def mailbox_managed_recover_cli(
    workspace: str = typer.Option(".", "--workspace"),
    adapter: str = typer.Option(..., "--adapter"),
    native_id: str = typer.Option(..., "--native-tool-session-id"),
    session: str = typer.Option(..., "--session"),
):
    _managed_mailbox_action("recover", workspace, adapter, native_id, session)


@mailbox_app.command("managed-revoke")
def mailbox_managed_revoke_cli(
    workspace: str = typer.Option(".", "--workspace"),
    adapter: str = typer.Option(..., "--adapter"),
    native_id: str = typer.Option(..., "--native-tool-session-id"),
    session: str = typer.Option(..., "--session"),
):
    _managed_mailbox_action("revoke", workspace, adapter, native_id, session)


@mailbox_app.command("reconcile-bindings")
def mailbox_reconcile_bindings_cli():
    _print_json(reconcile_managed_mailbox_bindings())


@mailbox_app.command("phonebook")
def mailbox_phonebook_cli(
    workspace: str | None = typer.Option(None, "--workspace"),
    include_paths: bool = typer.Option(False, "--include-paths"),
    limit: int = typer.Option(500, "--limit", min=1, max=1000),
):
    ensure_operator_mailboxes()
    _print_json(list_phonebook(workspace, include_paths=include_paths, limit=limit))


@mailbox_app.command("lookup")
def mailbox_lookup_cli(
    address: str,
    include_path: bool = typer.Option(False, "--include-path"),
):
    ensure_operator_mailboxes()
    _print_json(lookup_mailbox(address, include_path=include_path))


@mailbox_app.command("send")
def mailbox_send_cli(
    recipient: list[str] = typer.Option(..., "--to", help="Recipient address. Repeatable."),
    subject: str = typer.Option(..., "--subject"),
    operation_id: str = typer.Option(..., "--operation-id"),
    workspace: str = typer.Option(".", "--workspace"),
    body: str = typer.Option("", "--body"),
    kind: str = typer.Option("info", "--kind"),
    sender: str | None = typer.Option(None, "--from"),
    session: str | None = typer.Option(None, "--session"),
    binding_file: Path | None = typer.Option(
        None,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
):
    from brains.control.durable_mail import send_mailbox_message

    binding_secret = read_mailbox_binding_file(binding_file) if binding_file else None
    _print_json(
        send_mailbox_message(
            workspace,
            recipient,
            subject,
            operation_id,
            body=body,
            kind=kind,
            sender_address=sender,
            sender_session_id=session,
            binding_secret=binding_secret,
        )
    )


@mailbox_app.command("broadcast")
def mailbox_broadcast_cli(
    subject: str = typer.Option(..., "--subject"),
    operation_id: str = typer.Option(..., "--operation-id"),
    workspace: str = typer.Option(".", "--workspace"),
    body: str = typer.Option("", "--body"),
    kind: str = typer.Option("info", "--kind"),
    sender: str | None = typer.Option(None, "--from"),
    session: str | None = typer.Option(None, "--session"),
    binding_file: Path | None = typer.Option(
        None,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
):
    from brains.control.durable_mail import broadcast_mailbox_message

    binding_secret = read_mailbox_binding_file(binding_file) if binding_file else None
    _print_json(
        broadcast_mailbox_message(
            workspace,
            subject,
            operation_id,
            body=body,
            kind=kind,
            sender_address=sender,
            sender_session_id=session,
            binding_secret=binding_secret,
        )
    )


@mailbox_app.command("reply")
def mailbox_reply_cli(
    in_reply_to: str = typer.Option(..., "--in-reply-to"),
    operation_id: str = typer.Option(..., "--operation-id"),
    workspace: str = typer.Option(".", "--workspace"),
    body: str = typer.Option("", "--body"),
    subject: str | None = typer.Option(None, "--subject"),
    kind: str = typer.Option("info", "--kind"),
    sender: str | None = typer.Option(None, "--from"),
    session: str | None = typer.Option(None, "--session"),
    binding_file: Path | None = typer.Option(
        None,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
):
    from brains.control.durable_mail import reply_mailbox_message

    binding_secret = read_mailbox_binding_file(binding_file) if binding_file else None
    _print_json(
        reply_mailbox_message(
            workspace,
            in_reply_to,
            operation_id,
            subject=subject,
            body=body,
            kind=kind,
            sender_address=sender,
            sender_session_id=session,
            binding_secret=binding_secret,
        )
    )


@mailbox_app.command("forward")
def mailbox_forward_cli(
    forwarded_from: str = typer.Option(..., "--forwarded-from"),
    recipient: list[str] = typer.Option(..., "--to", help="Recipient address. Repeatable."),
    operation_id: str = typer.Option(..., "--operation-id"),
    workspace: str = typer.Option(".", "--workspace"),
    body: str = typer.Option("", "--body"),
    subject: str | None = typer.Option(None, "--subject"),
    kind: str = typer.Option("info", "--kind"),
    sender: str | None = typer.Option(None, "--from"),
    session: str | None = typer.Option(None, "--session"),
    binding_file: Path | None = typer.Option(
        None,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
):
    from brains.control.durable_mail import forward_mailbox_message

    binding_secret = read_mailbox_binding_file(binding_file) if binding_file else None
    _print_json(
        forward_mailbox_message(
            workspace,
            forwarded_from,
            recipient,
            operation_id,
            subject=subject,
            body=body,
            kind=kind,
            sender_address=sender,
            sender_session_id=session,
            binding_secret=binding_secret,
        )
    )


@mailbox_app.command("inbox")
def mailbox_inbox_cli(
    address: str | None = typer.Option(None, "--address"),
    session: str | None = typer.Option(None, "--session"),
    binding_file: Path | None = typer.Option(
        None,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    mark_read: bool = typer.Option(False, "--mark-read/--no-mark-read"),
    include_read: bool = typer.Option(False, "--include-read"),
    after_delivery_id: int | None = typer.Option(None, "--after-delivery-id", min=0),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
):
    from brains.control.durable_mail import read_mailbox_inbox

    binding_secret = read_mailbox_binding_file(binding_file) if binding_file else None
    _print_json(
        read_mailbox_inbox(
            address=address,
            session_id=session,
            binding_secret=binding_secret,
            mark_read=mark_read,
            include_read=include_read,
            after_delivery_id=after_delivery_id,
            limit=limit,
        )
    )


@mailbox_app.command("sent")
def mailbox_sent_cli(
    address: str | None = typer.Option(None, "--address"),
    session: str | None = typer.Option(None, "--session"),
    binding_file: Path | None = typer.Option(
        None,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    after_message_id: int | None = typer.Option(None, "--after-message-id", min=0),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
):
    from brains.control.durable_mail import read_mailbox_sent

    binding_secret = read_mailbox_binding_file(binding_file) if binding_file else None
    _print_json(
        read_mailbox_sent(
            address=address,
            session_id=session,
            binding_secret=binding_secret,
            after_message_id=after_message_id,
            limit=limit,
        )
    )


@mailbox_app.command("thread")
def mailbox_thread_cli(
    thread_id: str,
    address: str | None = typer.Option(None, "--address"),
    session: str | None = typer.Option(None, "--session"),
    binding_file: Path | None = typer.Option(
        None,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    mark_read: bool = typer.Option(False, "--mark-read/--no-mark-read"),
):
    from brains.control.durable_mail import read_mailbox_thread

    binding_secret = read_mailbox_binding_file(binding_file) if binding_file else None
    _print_json(
        read_mailbox_thread(
            thread_id,
            address=address,
            session_id=session,
            binding_secret=binding_secret,
            mark_read=mark_read,
        )
    )


@mailbox_app.command("notification-take")
def mailbox_notification_take_cli(
    session: str = typer.Option(..., "--session"),
    binding_file: Path = typer.Option(
        ...,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    notification_id: str | None = typer.Option(None, "--notification-id"),
    wait_ms: int = typer.Option(0, "--wait-ms", min=0, max=30_000),
):
    from brains.control.durable_mail import take_mailbox_notification

    _print_json(
        take_mailbox_notification(
            session,
            read_mailbox_binding_file(binding_file),
            notification_id=notification_id,
            wait_ms=wait_ms,
        )
    )


@mailbox_app.command("notification-settle")
def mailbox_notification_settle_cli(
    notification_id: str,
    session: str = typer.Option(..., "--session"),
    status: str = typer.Option(..., "--status"),
    binding_file: Path = typer.Option(
        ...,
        "--binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    error_code: str | None = typer.Option(None, "--error-code"),
):
    from brains.control.durable_mail import settle_mailbox_notification

    _print_json(
        settle_mailbox_notification(
            session,
            read_mailbox_binding_file(binding_file),
            notification_id,
            status=status,
            error_code=error_code,
        )
    )


@app.command("session-link-successor")
def session_link_successor_cli(
    from_session: str = typer.Option(..., "--from-session"),
    to_session: str = typer.Option(..., "--to-session"),
    tool: str | None = typer.Option(None, "--tool"),
    native_tool_session_id: str | None = typer.Option(None, "--native-tool-session-id"),
    mailbox_binding_file: Path | None = typer.Option(
        None,
        "--mailbox-binding-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    mailbox_notification_mode: str | None = typer.Option(None, "--mailbox-notification-mode"),
):
    from brains.control.sessions import link_session_successor

    mailbox_binding_secret = (
        read_mailbox_binding_file(mailbox_binding_file)
        if mailbox_binding_file is not None
        else None
    )
    _print_json(
        link_session_successor(
            from_session,
            to_session,
            tool=tool,
            native_tool_session_id=native_tool_session_id,
            mailbox_binding_secret=mailbox_binding_secret,
            mailbox_notification_mode=mailbox_notification_mode,
        )
    )


@app.command("session-end")
def session_end_cli(session: str = typer.Option(...), summary: str = ""):
    _print_json(end_session(session, summary))


@app.command("sessions")
def sessions_cli(workspace: str = ".", limit: int = 50):
    _print_json(list_sessions(workspace, limit=limit))


@app.command("session-message")
def session_message_cli(
    session: str = typer.Option(..., help="Session id to message."),
    text: str = typer.Option(..., help="The message to deliver to the running agent."),
    operation_id: str | None = typer.Option(
        None,
        help="Idempotency handle; re-sending the same one never queues a second message.",
    ),
):
    """Queue a durable message for a running Session (BL-P0-05).

    The command is recorded before it is delivered and settled with the
    outcome a consumer actually observed - including ``unsupported``, which is
    what an agent CLI with no open input channel truthfully reports.
    """
    from brains.control import session_commands as commands_ctl

    command, created = commands_ctl.enqueue(
        session,
        commands_ctl.KIND_MESSAGE,
        text=text,
        operation_id=operation_id,
        requested_by="cli",
    )
    _print_json({**command, "duplicate": not created})


@app.command("session-stop")
def session_stop_cli(
    session: str = typer.Option(..., help="Session id to stop."),
    reason: str = typer.Option("", help="Why the Session is being stopped."),
    operation_id: str | None = typer.Option(
        None,
        help="Idempotency handle; omitted, a repeated stop is the same logical command.",
    ),
):
    """Request that a Session's agent process be stopped (idempotent)."""
    from brains.control import session_commands as commands_ctl
    from brains.exec import session_dispatch

    command, created = commands_ctl.enqueue(
        session,
        commands_ctl.KIND_STOP,
        reason=reason or None,
        operation_id=operation_id,
        requested_by="cli",
    )
    if created and command["status"] == commands_ctl.STATUS_REQUESTED:
        session_dispatch.dispatch_owned(session_id=session)
        command = commands_ctl.get(command["command_id"]) or command
    _print_json({**command, "duplicate": not created})


@app.command("session-commands")
def session_commands_cli(
    session: str = typer.Option(..., help="Session id."),
    limit: int = 100,
):
    """The durable message/stop history for a Session."""
    from brains.control import session_commands as commands_ctl

    _print_json(commands_ctl.list_for_session(session, limit=limit))


@app.command("event-append")
def event_append_cli(
    kind: str = typer.Option(...),
    message: str = typer.Option(...),
    session: str | None = None,
):
    row = append_event(kind, message, session_id=session)
    _print_json({"id": row.id, "kind": row.kind})


@app.command("events")
def events_cli(limit: int = 100):
    rows = list_events(limit=limit)
    _print_json(
        [
            {
                "id": row.id,
                "kind": row.kind,
                "message": row.message,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    )


@app.command("event-context")
def event_context_cli(event_id: int):
    """Show one event's taxonomy and scope provenance."""
    from brains.control.events import get_event_context

    _print_json(get_event_context(event_id))


@app.command("event-scope")
def event_scope_cli():
    """Show typed/global/unresolved event-scope posture."""
    from brains.control.events import event_scope_report

    _print_json(event_scope_report())


@app.command("learn")
def learn_cli(
    workspace: str | None = None,
    apply: bool = typer.Option(False, "--apply", help="Write proposals to the ledger."),
    limit: int = 20,
):
    """Preview human-gated knowledge proposals from recent coordination history."""
    _print_json(propose_from_history(workspace_path=workspace, apply=apply, limit=limit))


@app.command("decision-file")
def decision_file_cli(
    title: str = typer.Option(...),
    workspace: str = ".",
    body: str = "",
    proposed: str | None = None,
    session: str | None = None,
):
    _print_json(
        file_decision_request(
            workspace,
            title=title,
            body=body,
            proposed_answer=proposed,
            session_id=session,
        )
    )


@app.command("decision-list")
def decision_list_cli(workspace: str = ".", limit: int = 50):
    _print_json(list_open_decisions(workspace, limit=limit))


@app.command("decision-route")
def decision_route_cli(
    code: str = typer.Option(...),
    assigned_operator: str | None = typer.Option(None, "--assigned-operator"),
    clear_assignment: bool = typer.Option(False, "--clear-assignment"),
    priority: str | None = typer.Option(None, "--priority"),
    due_at: str | None = typer.Option(None, "--due-at"),
    clear_due: bool = typer.Option(False, "--clear-due"),
    escalation_level: int | None = typer.Option(None, "--escalation-level"),
    escalation_reason: str = typer.Option("", "--escalation-reason"),
    operator: str | None = typer.Option(None, "--operator"),
):
    """Assign, prioritize, deadline, or escalate an open approval."""
    from brains.authz.resolver import resolve_local_principal
    from brains.control.decisions import route_decision

    _print_json(
        route_decision(
            code,
            assigned_operator=assigned_operator,
            clear_assignment=clear_assignment,
            priority=priority,
            due_at=due_at,
            clear_due=clear_due,
            escalation_level=escalation_level,
            escalation_reason=escalation_reason,
            principal=resolve_local_principal(operator=operator),
        )
    )


@app.command("decision-escalate")
def decision_escalate_cli(
    code: str = typer.Option(...),
    reason: str = typer.Option(..., "--reason"),
    assigned_operator: str | None = typer.Option(None, "--assigned-operator"),
    due_at: str | None = typer.Option(None, "--due-at"),
    operator: str | None = typer.Option(None, "--operator"),
):
    """Increment an open approval's escalation level with a reason."""
    from brains.authz.resolver import resolve_local_principal
    from brains.control.decisions import escalate_decision

    _print_json(
        escalate_decision(
            code,
            reason=reason,
            assigned_operator=assigned_operator,
            due_at=due_at,
            principal=resolve_local_principal(operator=operator),
        )
    )


@app.command("decision-resolve")
def decision_resolve_cli(
    code: str = typer.Option(...),
    chosen: str = typer.Option(...),
    reasoning: str = "",
    status: str = "resolved",
    operator: str | None = typer.Option(
        None,
        "--operator",
        help="Resolve as this operator instead of the shell's resolved actor. "
        "The resolver identity is recorded on the decision and audited.",
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help="The Session performing the resolution. A Session may not resolve "
        "the approval it requested.",
    ),
):
    """Resolve one approval request, with the resolver's identity bound.

    Separation of duty is enforced: a Runtime credential can never resolve, the
    Session that filed the ASK can never resolve it, and the Persona identity
    behind it can never resolve it (``brains.control.decisions``).
    """
    from brains.authz.resolver import resolve_local_principal
    from brains.control.decisions import ApprovalAuthorizationError

    principal = resolve_local_principal(operator=operator)
    try:
        result = resolve_decision(
            code,
            chosen,
            reasoning,
            status,
            principal=principal,
            resolving_session_id=session,
        )
    except ApprovalAuthorizationError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3)
    _print_json({**result, "resolved_by": principal.describe()})


@app.command("exec-session")
def exec_session_cli(
    prompt: str,
    workspace: str = ".",
    tool: str = "copilot",
    model: str | None = None,
    orient: str | None = None,
    operator: str | None = None,
):
    """Run a gated agent CLI session: the agent works freely in the workspace, but
    every OUTWARD action (push/deploy/DNS/money/remote/prod-network) is intercepted
    and blocked for your approval (`brains-ai decision-list` / `decision-resolve`).

    --orient seeds a brains code-orientation block prepended to the prompt (the
    capability-aware speedup for weak/cheap models). --tool one of copilot|claude|codex.
    """
    from brains.exec import run_session

    if tool == "copilot":
        argv = ["copilot", "-p", prompt, "--allow-all"]
        if model:
            argv += ["--model", model]
        feed = None  # copilot takes the prompt as an arg
    elif tool == "claude":
        argv = [
            "claude",
            "-p",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
        ]
        if model:
            argv += ["--model", model]
        feed = prompt
    elif tool == "codex":
        argv = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if model:
            argv += ["-m", model]
        feed = prompt
    else:
        raise typer.BadParameter("tool must be copilot|claude|codex")

    result = run_session(
        argv, workspace, prompt=feed, orient_query=orient, tool=tool, operator=operator
    )
    _print_json(result)


@app.command("handoff-set")
def handoff_set_cli(
    title: str = typer.Option(...),
    workspace: str = ".",
    body: str = "",
    session: str | None = None,
):
    _print_json(set_handoff(workspace, title, body, session_id=session))


@app.command("handoff-pick")
def handoff_pick_cli(workspace: str = ".", session: str | None = None):
    _print_json(pick_handoff(workspace, session_id=session))


@app.command("handoff-clear")
def handoff_clear_cli(
    workspace: str = ".",
    reason: str = "",
    session: str | None = None,
):
    _print_json(clear_handoff(workspace, reason, session_id=session))


@app.command("handoff-list")
def handoff_list_cli(workspace: str = ".", all: bool = False):
    _print_json(list_handoffs(workspace, active_only=not all))


@app.command("task-create")
def task_create_cli(
    title: str = typer.Option(...),
    workspace: str = ".",
    body: str = "",
    priority: str = "p2",
    depends_on: str = "",
    tags: str = "",
    session: str | None = None,
):
    _print_json(
        create_task(
            workspace,
            title=title,
            body=body,
            priority=priority,
            depends_on=depends_on,
            tags=tags,
            session_id=session,
        )
    )


@app.command("task-list")
def task_list_cli(
    workspace: str = ".",
    status: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
    limit: int = 100,
):
    _print_json(
        list_tasks(
            workspace_path=workspace,
            status=status,
            priority=priority,
            tags=tags,
            limit=limit,
        )
    )


@app.command("task-claim")
def task_claim_cli(code: str = typer.Option(...), session: str = typer.Option(...)):
    _print_json(claim_task(code, session_id=session))


@app.command("task-complete")
def task_complete_cli(
    code: str = typer.Option(...),
    session: str = typer.Option(...),
    summary: str = "",
):
    _print_json(complete_task(code, session_id=session, summary=summary))


@app.command("task-release")
def task_release_cli(
    code: str = typer.Option(...),
    session: str = typer.Option(...),
    reason: str = "",
):
    _print_json(release_task(code, session_id=session, reason=reason))


@app.command("task-handoff")
def task_handoff_cli(
    from_code: str = typer.Option(..., "--from-code"),
    title: str = typer.Option(...),
    session: str = typer.Option(...),
    body: str = "",
    priority: str = "p2",
    extra_depends_on: str = "",
    tags: str = "",
    summary: str = "",
):
    """Mark a task done and create a follow-up in one atomic step.

    The new task's ``depends_on`` is auto-prepended with ``--from-code``
    so a receiving agent cannot start it before the predecessor is
    recorded done. Same authority rules as ``task-complete``.
    """
    _print_json(
        handoff_task(
            from_code,
            title=title,
            session_id=session,
            body=body,
            priority=priority,
            extra_depends_on=extra_depends_on,
            tags=tags,
            completion_summary=summary,
        )
    )


@app.command("workspace-claim")
def workspace_claim_cli(
    workspace: str = ".",
    session: str = typer.Option(...),
    scope: str = "code",
    minutes: int = 30,
):
    _print_json(
        claim_workspace(
            workspace,
            session_id=session,
            scope=scope,
            duration_minutes=minutes,
        )
    )


@app.command("workspace-release")
def workspace_release_cli(workspace: str = ".", session: str = typer.Option(...)):
    _print_json(release_workspace(workspace, session_id=session))


@app.command("workspace-claims")
def workspace_claims_cli(workspace: str = ".", include_expired: bool = False):
    _print_json(list_workspace_claims(workspace, include_expired=include_expired))


workspaces_app = typer.Typer(help="Bulk operations on the workspaces table (prune, list, etc.).")
app.add_typer(workspaces_app, name="workspaces")


def _workspace_cascade(session):
    """Schema-derived cascade for everything that depends on a Workspace.

    Derived from the database's own ``PRAGMA foreign_key_list`` graph rather
    than a hand-maintained table list, so a new model with a Workspace or
    Session foreign key is swept automatically instead of silently leaving
    orphans behind (BL-P0-07).
    """
    import sqlite3

    from brains.storage.integrity import (
        UnsupportedDatabaseError,
        workspace_cascade_tables,
    )

    raw = session.connection().connection
    conn = getattr(raw, "driver_connection", None) or getattr(raw, "connection", raw)
    if not isinstance(conn, sqlite3.Connection):
        raise UnsupportedDatabaseError(
            "workspace cleanup derives its dependency order from SQLite schema "
            f"introspection (got {type(conn).__name__})"
        )
    return workspace_cascade_tables(conn)


def _apply_workspace_cascade(session, ids: list[int]) -> dict[str, int]:
    """Delete ``ids`` and every dependent row, deepest dependant first."""
    from sqlalchemy import text

    params = {f"id_{i}": id_val for i, id_val in enumerate(ids)}
    placeholders = ",".join(f":id_{i}" for i in range(len(ids)))
    root_predicate = f"id IN ({placeholders})"
    affected: dict[str, int] = {}
    for step in _workspace_cascade(session):
        result = session.execute(text(step.sql(root_predicate)), params)
        affected[step.table] = affected.get(step.table, 0) + (result.rowcount or 0)
    result = session.execute(text(f"DELETE FROM workspaces WHERE {root_predicate}"), params)
    affected["workspaces"] = result.rowcount or 0
    session.commit()
    return affected


@workspaces_app.command("prune")
def workspaces_prune_cli(
    slug_prefix: list[str] = typer.Option(
        None,
        "--slug-prefix",
        help="Match workspaces whose slug starts with this (repeatable). e.g. --slug-prefix test- --slug-prefix adopt-",
    ),
    path_contains: list[str] = typer.Option(
        None,
        "--path-contains",
        help="Match workspaces whose path contains this substring (repeatable). e.g. --path-contains pytest",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually delete. Without this flag the command is a dry-run (default).",
    ),
    limit_sample: int = typer.Option(
        20, "--sample", help="How many matched rows to show in the dry-run report."
    ),
) -> None:
    """Prune workspaces matching slug-prefix and/or path-substring patterns.

    Removes the matched ``workspaces`` rows and every row that cannot exist
    without them (memberships, sessions, claims, events, tasks, handoffs,
    snapshots, and their own dependants), using the cascade
    derived from the database's foreign keys. Records that have their own
    identity and only reference the Workspace optionally — Projects, Issues,
    Personas, knowledge, audit entries — keep their row and lose the reference,
    so the cleanup leaves neither orphans nor collateral damage.

    Default mode is dry-run — pass ``--apply`` to actually commit. Use this
    to clean up the 1k+ pytest fixture workspaces that historically
    polluted the live DB. The conftest fix prevents future recurrence.
    """
    from sqlalchemy import or_

    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import Workspace

    slug_prefix = list(slug_prefix or [])
    path_contains = list(path_contains or [])

    if not slug_prefix and not path_contains:
        typer.echo(
            "error: pass at least one --slug-prefix or --path-contains. "
            "Refusing to operate on every workspace.",
            err=True,
        )
        raise typer.Exit(code=2)

    init_db()
    with SessionLocal() as session:
        clauses = []
        for prefix in slug_prefix:
            clauses.append(Workspace.slug.ilike(f"{prefix}%"))
        for substr in path_contains:
            clauses.append(Workspace.path.ilike(f"%{substr}%"))
        matches = (
            session.query(Workspace.id, Workspace.slug, Workspace.path)
            .filter(or_(*clauses))
            .order_by(Workspace.slug.asc())
            .all()
        )
        ids = [m.id for m in matches]

        report: dict[str, Any] = {
            "matched": len(matches),
            "patterns": {
                "slug_prefix": slug_prefix,
                "path_contains": path_contains,
            },
            "dry_run": not apply,
            "sample": [{"slug": m.slug, "path": m.path} for m in matches[:limit_sample]],
        }

        if not matches:
            _print_json(report)
            return

        if not apply:
            typer.echo(
                f"DRY-RUN: would delete {len(matches)} workspaces (+ dependent rows in "
                f"{len(_workspace_cascade(session))} schema-derived tables). "
                "Pass --apply to commit.",
                err=True,
            )
            _print_json(report)
            return

        # APPLY ----
        deleted_by_table = _apply_workspace_cascade(session, ids)

        report["deleted_by_table"] = deleted_by_table
        report["deleted"] = deleted_by_table["workspaces"]
        _print_json(report)


@workspaces_app.command("doctor")
def workspaces_doctor_cli(
    prune_missing: bool = typer.Option(
        False,
        "--prune-missing",
        help="Delete workspace rows whose .path no longer exists on disk (with full cascade).",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Required with --prune-missing to actually delete. Without it, --prune-missing is a dry-run.",
    ),
    archive_missing: bool = typer.Option(
        False,
        "--archive-missing",
        help="Archive missing workspace identities without deleting their durable history.",
    ),
) -> None:
    """Audit the workspaces table for fishy rows.

    Reports two categories:
      * **missing** — workspace.path no longer exists on disk (stale orphan)
      * **no_marker** — path exists but has no project marker (.git,
        pyproject.toml, package.json, Cargo.toml, go.mod, etc.). Usually an
        umbrella parent folder that an agent registered by accident.

    Read-only by default. Prefer ``--archive-missing --apply`` to remove stale
    paths from active views while preserving Sessions, events, handoffs, and
    governance history. ``--prune-missing --apply`` remains the explicit
    destructive cleanup path.
    """
    import os as _os

    from brains.control.sessions import has_project_marker
    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import Workspace, WorkspaceAlias

    def promote_primary(workspace: Workspace, path: str) -> None:
        conflict = (
            session.query(Workspace)
            .filter(Workspace.path == path, Workspace.id != workspace.id)
            .one_or_none()
        )
        if conflict is None:
            workspace.path = path
            return
        previous = workspace.path
        workspace.path = f"__brains_path_swap__/{workspace.id}/{conflict.id}"
        session.flush()
        conflict.path = previous
        session.flush()
        workspace.path = path

    if archive_missing and prune_missing:
        typer.echo(
            "error: choose --archive-missing or --prune-missing, not both.",
            err=True,
        )
        raise typer.Exit(code=2)

    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(Workspace)
            .filter(Workspace.status == "active")
            .order_by(Workspace.slug.asc())
            .all()
        )
        aliases: dict[int, list[str]] = {}
        for workspace_id, path in session.query(
            WorkspaceAlias.workspace_id, WorkspaceAlias.path
        ).filter(WorkspaceAlias.workspace_id.in_([row.id for row in rows])):
            aliases.setdefault(workspace_id, []).append(path)
        missing: list[dict[str, Any]] = []
        no_marker: list[dict[str, Any]] = []
        primary_promotions: dict[int, str] = {}
        ok = 0
        for row in rows:
            paths = list(dict.fromkeys([row.path, *aliases.get(row.id, [])]))
            usable = [path for path in paths if path and _os.path.isdir(path)]
            if not usable:
                missing.append({"id": row.id, "slug": row.slug, "path": row.path})
            else:
                selected = row.path if row.path in usable else usable[0]
                if selected != row.path:
                    primary_promotions[row.id] = selected
                if not any(has_project_marker(path) for path in usable):
                    no_marker.append({"id": row.id, "slug": row.slug, "path": selected})
                else:
                    ok += 1

        report: dict[str, Any] = {
            "total": len(rows),
            "ok": ok,
            "missing_count": len(missing),
            "no_marker_count": len(no_marker),
            "missing": missing,
            "no_marker": no_marker,
            "pruned_missing": None,
            "archived_missing": None,
        }

        if archive_missing and missing:
            if not apply:
                typer.echo(
                    f"DRY-RUN: would archive {len(missing)} missing-on-disk workspaces. "
                    "Pass --apply to commit.",
                    err=True,
                )
                report["archived_missing"] = {
                    "dry_run": True,
                    "would_archive": len(missing),
                }
                _print_json(report)
                return
            archived = (
                session.query(Workspace)
                .filter(Workspace.id.in_([m["id"] for m in missing]))
                .update({Workspace.status: "archived"}, synchronize_session=False)
            )
            session.commit()
            report["archived_missing"] = {"dry_run": False, "archived": archived}

        if prune_missing and missing:
            if not apply:
                typer.echo(
                    f"DRY-RUN: would delete {len(missing)} missing-on-disk "
                    f"workspaces (+ dependent rows in {len(_workspace_cascade(session))} "
                    "schema-derived tables). Pass --apply to commit.",
                    err=True,
                )
                report["pruned_missing"] = {
                    "dry_run": True,
                    "would_delete": len(missing),
                }
                _print_json(report)
                return

            deleted_by_table = _apply_workspace_cascade(session, [m["id"] for m in missing])

            report["pruned_missing"] = {
                "dry_run": False,
                "deleted": deleted_by_table["workspaces"],
                "deleted_by_table": deleted_by_table,
            }

        if apply and (archive_missing or prune_missing) and primary_promotions:
            for workspace_id, path in primary_promotions.items():
                workspace = session.get(Workspace, workspace_id)
                if workspace is not None:
                    promote_primary(workspace, path)
            session.commit()

        _print_json(report)


@app.command("message-send")
def message_send_cli(
    subject: str = typer.Option(...),
    body: str = "",
    from_session: str | None = None,
    to_session: str | None = None,
    workspace: str | None = None,
    kind: str = "info",
    route_to_current: bool = typer.Option(
        False,
        "--route-to-current",
        help="If to_session is ended and its workspace has exactly one live "
        "session, deliver there instead (explicit opt-in; otherwise refused).",
    ),
):
    _print_json(
        send_message(
            subject=subject,
            body=body,
            from_session_id=from_session,
            to_session_id=to_session,
            workspace_path=workspace,
            kind=kind,
            route_to_current=route_to_current,
        )
    )


@app.command("message-read")
def message_read_cli(
    session: str = typer.Option(...),
    mark_read: bool = True,
    include_read: bool = False,
    limit: int = 50,
    after_id: int | None = typer.Option(None, "--after-id"),
):
    _print_json(
        read_messages(
            session,
            mark_read=mark_read,
            include_read=include_read,
            limit=limit,
            after_id=after_id,
        )
    )


@app.command("snapshot-capture")
def snapshot_capture_cli(
    kind: str = typer.Option(...),
    data: str = typer.Option(...),
    workspace: str = ".",
    session: str | None = None,
):
    _print_json(capture_snapshot(workspace, kind=kind, data=data, session_id=session))


@app.command("snapshot-latest")
def snapshot_latest_cli(kind: str = typer.Option(...), workspace: str = "."):
    _print_json(latest_snapshot(workspace, kind=kind))


@app.command("pattern-propose")
def pattern_propose_cli(
    name: str = typer.Option(...),
    category: str = typer.Option(...),
    description: str = typer.Option(...),
    example: str = "",
    applies_to: str = "",
    session: str | None = None,
):
    _print_json(
        propose_pattern(
            name=name,
            category=category,
            description=description,
            example=example,
            applies_to=applies_to,
            session_id=session,
        )
    )


@app.command("pattern-approve")
def pattern_approve_cli(name: str = typer.Option(...), approved: bool = True):
    _print_json(approve_pattern(name, approved=approved))


@app.command("pattern-list")
def pattern_list_cli(
    category: str | None = None,
    status: str = "approved",
    limit: int = 100,
):
    _print_json(list_patterns(category=category, status=status, limit=limit))


@app.command("pattern-use")
def pattern_use_cli(name: str = typer.Option(...)):
    _print_json(use_pattern(name))


@app.command("tool-register")
def tool_register_cli(
    name: str = typer.Option(...),
    display_name: str = typer.Option(...),
    cli_command: str = typer.Option(...),
    spawn_args: str = "",
    capabilities: str = "",
    notes: str = "",
    verify: bool = True,
):
    _print_json(
        register_tool(
            name=name,
            display_name=display_name,
            cli_command=cli_command,
            spawn_args=spawn_args,
            capabilities=capabilities,
            notes=notes,
            verify=verify,
        )
    )


@app.command("tool-list")
def tool_list_cli(verify_now: bool = False):
    _print_json(list_registered_tools(verify_now=verify_now))


@app.command("tool-verify")
def tool_verify_cli(name: str = typer.Option(...)):
    _print_json(verify_tool(name))


@app.command("recurring-create")
def recurring_create_cli(
    name: str = typer.Option(...),
    title_template: str = typer.Option(...),
    workspace: str = ".",
    body_template: str = "",
    priority: str = "p2",
    tags: str = "",
    cron_expr: str = "manual",
    session: str | None = None,
    spawn_tool: str | None = None,
    spawn_args: str | None = None,
    spawn_prompt: str | None = None,
):
    _print_json(
        create_recurring_task(
            workspace,
            name=name,
            title_template=title_template,
            body_template=body_template,
            priority=priority,
            tags=tags,
            cron_expr=cron_expr,
            session_id=session,
            spawn_tool=spawn_tool,
            spawn_args=spawn_args,
            spawn_prompt=spawn_prompt,
        )
    )


@app.command("recurring-list")
def recurring_list_cli(
    workspace: str = ".",
    enabled: bool | None = None,
    limit: int = 100,
):
    _print_json(list_recurring_tasks(workspace, enabled=enabled, limit=limit))


@app.command("recurring-enable")
def recurring_enable_cli(name: str = typer.Option(...), enabled: bool = True):
    _print_json(set_recurring_enabled(name, enabled=enabled))


@app.command("recurring-fire")
def recurring_fire_cli(
    name: str = typer.Option(...),
    session: str | None = None,
):
    _print_json(fire_recurring_task(name, session_id=session))


@app.command("views-refresh")
def views_refresh_cli(workspace: str = "."):
    _print_json(refresh_views(workspace))


@jobs_app.command("list")
def jobs_list_cli():
    _print_json(list_jobs())


@jobs_app.command("run")
def jobs_run_cli(name: str, workspace: str = "."):
    _print_json(run_job(name, workspace_path=workspace))


@app.command("audit-list")
def audit_list_cli(
    limit: int = typer.Option(100, help="Max entries (1-1000)"),
    since_id: int | None = typer.Option(None, help="Only entries with id > since-id"),
    action_prefix: str | None = typer.Option(
        None, help="LIKE filter on action (e.g. 'provider.' or 'task.')"
    ),
    actor: str | None = typer.Option(None, help="Exact actor match"),
):
    """List signed audit-log entries, newest first."""
    from brains.audit import list_entries

    _print_json(
        {
            "entries": list_entries(
                limit=limit,
                since_id=since_id,
                action_prefix=action_prefix,
                actor=actor,
            )
        }
    )


@app.command("audit-verify")
def audit_verify_cli():
    """Recompute the audit chain and report the first divergence, if any.

    Exits 0 when the chain is intact, 1 when tamper is detected. Fails
    closed on a divergence, a missing chain head, or a count that does not
    match the head - a report that cannot prove the chain is whole is not
    a pass. The JSON output is the same shape returned by the
    ``audit_verify`` MCP tool.
    """
    from brains.audit import chain_status

    status = chain_status()
    _print_json(status)
    if not status["ok"]:
        raise typer.Exit(code=1)


@app.command("audit-adopt")
def audit_adopt_cli(
    actor: str = typer.Option("operator", help="Who is adopting the chain (recorded)."),
):
    """Adopt a genuine pre-signature audit chain: verify it, then sign it once.

    A store written before the chain head was signed has an unsigned head,
    which now fails verification and refuses every append - an unsigned head
    over a non-empty log cannot prove the log was not truncated. This command
    is the one path out of that state, and it is deliberately an explicit
    operator gesture: it verifies every entry, the head triple and the append
    count *before* signing, refuses a store that already carries an adoption
    entry, and signs, marks and records the adoption in one transaction.

    Exits 0 when the store is (now) adopted, 1 when adoption is refused -
    which means the chain does not verify and is a tamper report, not a
    migration step.
    """
    from brains.audit import AuditWriteError, adopt_legacy_chain, chain_status

    try:
        outcome = adopt_legacy_chain(actor=actor)
    except AuditWriteError as exc:
        _print_json({"ok": False, "error": str(exc), "status": chain_status()})
        raise typer.Exit(code=1) from exc
    _print_json({"ok": True, **outcome, "chain": chain_status()})


@app.command("governed-list")
def governed_list_cli(
    limit: int = typer.Option(50, help="Max rows (1-500)"),
    status: str | None = typer.Option(
        None,
        help="Filter by status: requested|pending|authorized|executing|succeeded|failed|denied|expired",
    ),
    actor: str | None = typer.Option(None, help="Exact actor match"),
    action_prefix: str | None = typer.Option(None, help="LIKE filter on the governed action name"),
):
    """List governed actions (the decision spine behind every outward effect)."""
    from brains.govern import list_governed_actions

    _print_json(
        {
            "actions": list_governed_actions(
                limit=limit,
                status=status,
                actor=actor,
                action_prefix=action_prefix,
            )
        }
    )


@app.command("governed-sweep")
def governed_sweep_cli():
    """Settle governed actions whose approval window or attempt lease expired.

    The same maintenance the recurring scheduler runs each tick, exposed for a
    host that does not run the scheduler. It settles only attempts whose
    per-attempt lease has expired, and only while their status and attempt are
    still the ones it read, so a live execution is never swept.
    """
    from brains.govern import run_maintenance

    _print_json(run_maintenance())


@app.command("backup")
def backup_cli(
    out_path: str = typer.Argument(
        ..., help="Destination .tar.gz path (parent directories are created)."
    ),
):
    """Create a backup archive of the current brains DB.

    Dispatches on ``subsystems.storage.backend``: SQLite uses the
    stdlib online backup API (safe even while writers are active);
    Postgres shells out to ``pg_dump`` (must be on PATH). Records
    ``admin.backup_created.attempted`` before it runs and
    ``admin.backup_created`` once the archive exists; refuses to run at all
    if the attempt cannot be recorded.
    """
    from dataclasses import asdict

    from brains.audit import AuditWriteError, required_effect
    from brains.backup import BackupError, BackupToolUnavailable, create_backup

    try:
        with required_effect(
            actor="admin",
            action="admin.backup_created",
            payload={"out_path": str(out_path)},
        ) as effect:
            result = create_backup(out_path)
            payload = asdict(result)
            effect.record_outcome(payload)
    except BackupToolUnavailable as exc:
        typer.echo(f"backup tool unavailable: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except BackupError as exc:
        typer.echo(f"backup failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except AuditWriteError as exc:
        typer.echo(f"backup refused: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _print_json(payload)


@app.command("restore")
def restore_cli(
    archive_path: str = typer.Argument(..., help="Path to the .tar.gz backup archive."),
    target_url: str | None = typer.Option(
        None, "--target-url", help="Override the current settings.db_url."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the interactive confirmation prompt. REQUIRED for non-interactive use.",
    ),
):
    """Restore a brains DB from a backup archive.

    Destructive: overwrites the on-disk SQLite file (or replays the
    SQL dump into the Postgres DB). Run ``brains-ai backup`` first.
    Records ``admin.restore_run.attempted`` before it touches anything -
    a restore whose attempt cannot be recorded does not run - and
    ``admin.restore_run`` once the restore returned.

    One thing this ordering cannot buy, and does not claim: the attempt entry
    is written into the store the restore then replaces, so it survives in the
    pre-restore database rather than in the restored one. The restored chain
    carries the completion entry, and the attempt is where it was written.
    """
    from dataclasses import asdict

    from brains.audit import AuditWriteError, required_effect
    from brains.backup import (
        BackupError,
        BackupToolUnavailable,
        inspect_archive,
        restore_backup,
    )

    try:
        manifest = inspect_archive(archive_path)
    except BackupError as exc:
        typer.echo(f"cannot read archive: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"About to restore from {archive_path}")
    typer.echo(f"  backend:       {manifest.get('backend')}")
    typer.echo(f"  created_at:    {manifest.get('created_at')}")
    typer.echo(f"  source_db:     {manifest.get('sanitized_db_url')}")
    typer.echo(f"  blob bytes:    {manifest.get('data_size_bytes')}")
    typer.echo(f"  schema rows:   {len(manifest.get('schema_versions', []))}")
    if not yes:
        confirmed = typer.confirm("This will OVERWRITE the current DB. Continue?")
        if not confirmed:
            typer.echo("aborted", err=True)
            raise typer.Exit(code=2)

    try:
        with required_effect(
            actor="admin",
            action="admin.restore_run",
            payload={
                "archive_path": str(archive_path),
                "target_url": bool(target_url),
                "source_db": manifest.get("sanitized_db_url"),
            },
        ) as effect:
            result = restore_backup(archive_path, target_url=target_url)
            payload = asdict(result)
            effect.record_outcome(payload)
    except BackupToolUnavailable as exc:
        typer.echo(f"restore tool unavailable: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except BackupError as exc:
        typer.echo(f"restore failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except AuditWriteError as exc:
        typer.echo(f"restore refused: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _print_json(payload)


@app.command("backup-inspect")
def backup_inspect_cli(archive_path: str = typer.Argument(...)):
    """Print the manifest of a backup archive without restoring."""
    from brains.backup import BackupError, inspect_archive

    try:
        _print_json(inspect_archive(archive_path))
    except BackupError as exc:
        typer.echo(f"cannot inspect archive: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# --------------------------------------------------------------------------- #
# db — SQLite integrity diagnosis, repair, and backup verification (BL-P0-07)
# --------------------------------------------------------------------------- #

db_app = typer.Typer(
    help="Diagnose and repair the state database: migration status, integrity "
    "checks, foreign-key checks, product invariants, and verified backups."
)
app.add_typer(db_app, name="db")


@db_app.command("migrations")
def db_migrations_cli() -> None:
    """Report migration readiness without applying anything (read-only).

    Prints the ordered ledger with each migration's status (``applied``,
    ``skipped``, ``failed``, ``running``), the backend it ran on, its recorded
    checksum and whether that checksum was recorded by this runner or adopted
    from a pre-checksum ledger, attempt count, timings, and the error text of
    any attempt that rolled back. Findings name edited migrations, ledger gaps,
    interrupted runs, and migrations the ledger knows but this build does not.

    The database is identified by backend, host and name only; no credentials
    are printed.

    Exits 1 when the store is not migration-healthy - anything pending, failed,
    or a schema that does not contain every model-declared object - so a
    pipeline cannot treat an unmigrated store as ready.
    """
    from brains.storage.migrations import migration_status

    payload = migration_status()
    _print_json(payload)
    if not payload.get("healthy"):
        raise typer.Exit(code=1)


@db_app.command("migrate")
def db_migrate_cli() -> None:
    """Apply every pending migration, in order, and verify the result.

    Each delta runs in its own transaction and is recorded ``applied`` only
    after it commits. A migration whose file changed since it was applied, a
    migration with no implementation for this backend, and a schema still
    missing model-declared objects are all refusals, not warnings.

    Exits 2 when the runner refuses, 1 when the store is still not healthy
    afterwards.
    """
    from brains.storage.migrations import (
        MigrationCorpusError,
        MigrationError,
        run_migrations,
    )

    try:
        report = run_migrations(apply=True)
    except (MigrationError, MigrationCorpusError) as exc:
        typer.echo(f"migration refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _print_json(report.to_dict())
    if not report.healthy:
        raise typer.Exit(code=1)


@db_app.command("diagnose")
def db_diagnose_cli() -> None:
    """Report SQLite and product-invariant integrity findings (read-only).

    Runs ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check`` plus the
    Brains invariants: contradictory terminal Session state, Org-less
    Workspaces, and orphaned or expired Session claims. Rows whose correct
    value cannot be derived from stored evidence are reported as
    ``ambiguous_legacy`` rather than guessed at.

    Never migrates or writes: it opens the database file read-only, so it
    works even when foreign-key enforcement is refusing new connections.

    Exits 1 when any finding is present *or* when any check could not run on
    this schema (``complete: false``): an unexamined invariant is unknown, not
    clean, so the report fails closed rather than implying coverage it does
    not have.
    """
    from brains.storage.integrity import IntegrityError, diagnose_database

    try:
        report = diagnose_database()
    except IntegrityError as exc:
        typer.echo(f"diagnosis failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _print_json(report.to_dict())
    if not report.ok:
        raise typer.Exit(code=1)


@db_app.command("fk-check")
def db_fk_check_cli() -> None:
    """Check whether foreign-key enforcement can be turned on safely.

    Exits 0 when ``PRAGMA foreign_key_check`` is clean, 1 when violations
    remain. Enforcement (``BRAINS_SQLITE_ENFORCE_FOREIGN_KEYS=1``) refuses to
    start over a store that fails this check. Read-only.
    """
    from brains.storage.integrity import (
        ForeignKeyViolationsError,
        IntegrityError,
        assert_foreign_keys_clean,
        open_database,
        resolve_sqlite_path,
    )

    database = "(unresolved)"
    try:
        database = str(resolve_sqlite_path())
        with open_database(database, read_only=True) as conn:
            assert_foreign_keys_clean(conn)
    except ForeignKeyViolationsError as exc:
        _print_json({"database": database, "clean": False, "detail": str(exc)})
        raise typer.Exit(code=1) from exc
    except IntegrityError as exc:
        typer.echo(f"foreign-key check failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _print_json({"database": database, "clean": True})


@db_app.command("verify-backup")
def db_verify_backup_cli(
    archive_path: str = typer.Argument(..., help="Path to the .tar.gz backup archive."),
    expect_source: str | None = typer.Option(
        None,
        "--expect-source",
        help="Require the archive to have been taken from this SQLite file and to "
        "still represent its current content.",
    ),
) -> None:
    """Restore a backup into an isolated temp directory and verify it.

    Nothing outside the temporary directory is written. A backup is not
    valid until this passes; ``db repair --apply`` refuses to run without it.
    With ``--expect-source`` the archive must also still match the live
    database's schema *and* content fingerprint, so an archive that a later
    write has superseded is reported as stale instead of being trusted.
    """
    from brains.backup import BackupError, verify_backup

    try:
        verification = verify_backup(archive_path, expected_source_path=expect_source)
    except BackupError as exc:
        typer.echo(f"cannot verify archive: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _print_json(verification.to_dict())
    if not verification.ok:
        raise typer.Exit(code=1)


@db_app.command("repair")
def db_repair_cli(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually write the repair. Without this flag the command is a dry-run (default).",
    ),
    backup: str | None = typer.Option(
        None,
        "--backup",
        help="Existing backup archive to verify and use as the prerequisite for --apply.",
    ),
    backup_to: str | None = typer.Option(
        None,
        "--backup-to",
        help="Create a fresh backup at this path, verify it by isolated restore, then repair.",
    ),
    delete_orphans: bool = typer.Option(
        False,
        "--delete-orphans",
        help="Also delete orphaned rows whose required parent is gone and whose foreign "
        "key cannot be nulled. Uses the schema-derived cascade. Off by default.",
    ),
) -> None:
    """Repair deterministic integrity anomalies. Dry-run unless --apply.

    ``--apply`` takes the SQLite write lock before it does anything else and
    holds it across diagnosis, backup capture, backup verification, every
    repair pass, and the commit, so nothing can be written between the state
    the archive captured and the state the repair mutates. It requires a
    manifest backup verified by an isolated restore and proven to still
    represent the live database (pass ``--backup`` or ``--backup-to``),
    refuses to touch a database whose ``integrity_check`` is not ok, and
    commits every action in one transaction that re-plans until it converges
    and rolls back as a whole on failure. Ambiguous legacy rows and orphans
    that need an operator decision are reported, never guessed.

    Exits 1 when the post-repair diagnosis is not clean - remaining foreign-key
    violations, remaining findings of any kind, *or* invariant checks that
    could not run on this schema - so a repair that leaves work behind, or
    that could not see all of it, cannot pass silently in a pipeline.
    """
    from brains.audit import AuditWriteError, required_effect
    from brains.storage.integrity import (
        IntegrityError,
        repair_database,
        resolve_sqlite_path,
    )

    if not apply:
        try:
            payload = repair_database(
                apply=False,
                backup_archive=backup,
                backup_to=backup_to,
                delete_orphans=delete_orphans,
            )
        except IntegrityError as exc:
            typer.echo(f"repair refused: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(
            f"DRY-RUN: {len(payload['planned_actions'])} repair action(s) planned, "
            f"{len(payload['unrepaired'])} finding(s) need an operator decision. "
            "Pass --apply with --backup/--backup-to to commit.",
            err=True,
        )
        _print_json(payload)
        return

    # The repair holds the SQLite write lock across its whole transaction, so
    # an audit append cannot join it - the attempt is committed before the
    # lock is taken instead, and the outcome after it is released.
    try:
        with required_effect(
            actor="admin",
            action="admin.db_repaired",
            payload={
                "database": str(resolve_sqlite_path()),
                "delete_orphans": delete_orphans,
                "backup_archive": backup or backup_to,
            },
        ) as effect:
            payload = repair_database(
                apply=True,
                backup_archive=backup,
                backup_to=backup_to,
                delete_orphans=delete_orphans,
            )
            effect.record_outcome(
                {
                    "database": payload["database"],
                    "actions": [
                        {
                            "code": action["code"],
                            "table": action["table"],
                            "applied_rows": action["applied_rows"],
                        }
                        for action in payload["planned_actions"]
                    ],
                    "backup_archive": (payload["backup"] or {}).get("archive_path"),
                    "passes": payload.get("passes"),
                    "post_repair_ok": payload.get("post_repair", {}).get("ok"),
                }
            )
    except IntegrityError as exc:
        typer.echo(f"repair refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except AuditWriteError as exc:
        typer.echo(f"repair refused: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _print_json(payload)
    if payload["applied"] and payload.get("post_repair", {}).get("ok") is not True:
        # A repair that leaves anything behind - a foreign-key violation, a
        # finding it re-created, a row only an operator can resolve, or an
        # invariant this schema could not be checked against - is not a
        # success. The payload says exactly what remains (and what was never
        # examined); the exit code says that something does.
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# daemon — runtime daemon (native-battalion WS1)
# --------------------------------------------------------------------------- #


def _daemon_pidfile():
    from brains.api.admin_key import state_dir

    return state_dir() / "daemon.pid"


def _daemon_stopfile():
    from brains.api.admin_key import state_dir

    return state_dir() / "daemon.stop"


@daemon_app.command("detect")
def daemon_detect_cli(as_json: bool = typer.Option(False, "--json")):
    """One-off detection sweep (no register)."""
    from brains.daemon.config import load_config
    from brains.daemon.detect import detect_tools

    cfg = load_config()
    detected = detect_tools(cfg)
    if as_json:
        _print_json(detected)
    else:
        if not detected:
            typer.echo("no coding CLIs detected on PATH")
        for d in detected:
            typer.echo(f"{d['tool']:10} {d['version'] or '?':20} {d['binary']}")


@daemon_app.command("status")
def daemon_status_cli(as_json: bool = typer.Option(False, "--json")):
    """Local view: detected tools + this machine's runtimes (from the hub) +
    hub reachability."""
    from brains.daemon import Daemon
    from brains.daemon.config import load_config

    daemon = Daemon(load_config())
    status = daemon.status()
    if as_json:
        _print_json(status)
    else:
        typer.echo(f"machine    : {status['machine_id']} ({status['machine_label']})")
        typer.echo(f"hub        : {status['hub_url']} reachable={status['hub_reachable']}")
        typer.echo(f"detected   : {[d['tool'] for d in status['detected']]}")
        for rt in status["runtimes"]:
            typer.echo(
                f"  runtime {rt['id']:>4} {rt['slug']:30} {rt['status']:8} "
                f"{rt['health']} hb={rt['last_heartbeat_at']}"
            )


@daemon_app.command("drain")
def daemon_drain_cli():
    """Set all local runtimes -> draining (graceful; finish in-flight, accept no
    new assignments)."""
    from brains.daemon import Daemon
    from brains.daemon.config import load_config

    _print_json(Daemon(load_config()).drain())


@daemon_app.command("stop")
def daemon_stop_cli(force: bool = typer.Option(False, "--force")):
    """Signal a running foreground daemon to stop (drain or kill per --force).

    Refuses to signal a PID the pidfile names when it no longer verifiably
    identifies the daemon process that wrote it (BL-P1-09) — a reused PID
    would otherwise send the signal to an unrelated process. The stale
    pidfile is removed instead so a future ``daemon stop`` doesn't repeat
    the same mistake.
    """
    import os
    import signal

    from brains.service.common import (
        cleanup_stale_pidfile,
        current_platform,
        read_pidfile_record,
        verify_pid,
    )

    pidfile = _daemon_pidfile()
    if not pidfile.is_file():
        typer.echo("no daemon pidfile found (is a foreground daemon running?)", err=True)
        raise typer.Exit(code=1)
    check = verify_pid(read_pidfile_record(pidfile))
    if check["confidence"] in ("stale", "absent"):
        cleanup_stale_pidfile(pidfile)
        typer.echo(
            f"refusing to signal pid {check['pid']}: {check['reason']} (stale pidfile removed)",
            err=True,
        )
        raise typer.Exit(code=1)
    if check["confidence"] != "verified":
        typer.echo(
            f"refusing to signal pid {check['pid']}: process identity is "
            f"{check['confidence']} ({check['reason']})",
            err=True,
        )
        raise typer.Exit(code=1)
    pid = check["pid"]
    if current_platform() == "windows" and not force:
        stopfile = _daemon_stopfile()
        stopfile.parent.mkdir(parents=True, exist_ok=True)
        stopfile.write_text(str(pid), encoding="utf-8")
        typer.echo(f"requested graceful daemon stop for verified pid {pid}")
        return
    if current_platform() == "windows" and force:
        import subprocess

        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            typer.echo(
                f"failed to terminate pid {pid}: {completed.stderr or completed.stdout}",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(f"force-terminated daemon pid {pid}")
        return
    sig = signal.SIGTERM
    if force and hasattr(signal, "SIGKILL"):
        sig = signal.SIGKILL
    try:
        os.kill(pid, sig)
        typer.echo(f"signalled daemon pid {pid} ({'force' if force else 'graceful'})")
    except OSError as exc:
        typer.echo(f"failed to signal pid {pid}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@daemon_app.command("start")
def daemon_start_cli(
    hub_url: str = typer.Option(None, "--hub-url", "--hub"),
    operator_key: str = typer.Option(None, "--operator-key", "--key"),
    enrol: str = typer.Option(
        None,
        "--enrol",
        help="Enrolment token: connect this machine without an operator key (one-shot).",
    ),
    max_concurrent: int = typer.Option(None, "--max-concurrent"),
    working_root: str = typer.Option(None, "--working-root"),
    once: bool = typer.Option(
        False, "--once", help="Run a single poll-cycle and exit (useful in tests)."
    ),
):
    """Run the daemon loop (foreground). ``--once`` runs a single cycle.

    With ``--enrol <token>`` the daemon redeems an enrolment token to register
    this machine's CLIs WITHOUT an operator key, then exits (one-shot connect).
    """
    import contextlib
    import signal

    from brains.daemon import Daemon
    from brains.daemon.config import load_config

    cfg = load_config(
        hub_url=hub_url,
        operator_key=operator_key,
        max_concurrent=max_concurrent,
        working_root=working_root,
    )
    daemon = Daemon(cfg)
    if enrol:
        _print_json(daemon.enrol_once(enrol))
        return
    if once:
        _print_json(daemon.run_once())
        return

    pidfile = _daemon_pidfile()
    stopfile = _daemon_stopfile()
    with contextlib.suppress(OSError):
        stopfile.unlink()
    try:
        from brains.service.common import write_pidfile

        write_pidfile(pidfile)
    except OSError:
        pass

    def _handle(_signum, _frame):
        daemon.stop()

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
    typer.echo(f"brains daemon starting (hub={cfg.hub_url}, machine={cfg.machine_id})")
    try:
        daemon.run()
    finally:
        with contextlib.suppress(OSError):
            if pidfile.is_file():
                pidfile.unlink()


# --------------------------------------------------------------------------- #
# readiness / queue-health / recovery-policy — B8, BL-P1-09, BL-P1-12
#
# CLI mirrors of GET /v1/admin/readiness, GET/POST /v1/admin/queue-health(/repair)
# and GET /v1/admin/recovery-policy — the same control-layer functions the API
# calls, so a local operator gets the identical verdict without going through
# HTTP. These do not require an admin API key locally: like `db diagnose`/
# `db repair`, they run with the same trust boundary as any other local CLI
# invocation (the operating-system user), consistent with every other local
# diagnostic command in this file.
# --------------------------------------------------------------------------- #


@app.command("readiness")
def readiness_cli() -> None:
    """Report storage, queue, durable-mail, and recovery readiness (B8).

    Distinct from ``brains-ai health`` style liveness checks — this reports
    one overall ready/degraded verdict plus bounded, redacted per-component
    detail, and never fabricates a passing recovery-policy or queue state.
    Exits 1 when the overall verdict is degraded, so it composes in scripts.
    """
    from brains.control.operations import readiness_report

    report = readiness_report()
    _print_json(report)
    if report["status"] != "ready":
        raise typer.Exit(code=1)


queue_health_app = typer.Typer(
    help="Coordination queue health + continuity repair (BL-P1-12): family "
    "summary, orphan/stale detection, and dry-run/apply repair."
)
app.add_typer(queue_health_app, name="queue-health")


@queue_health_app.command("status")
def queue_health_status_cli() -> None:
    """Family summary (owner/scope/lifecycle/expiry + counts) plus bounded,
    non-destructive orphan/stale-lease diagnosis. Mutates nothing."""
    from brains.control.queue_health import diagnose, summarize

    _print_json({"summary": summarize(), "diagnosis": diagnose()})


@queue_health_app.command("repair")
def queue_health_repair_cli(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually perform the safe continuity repairs. Without this flag "
        "the command is a dry-run (default) that mutates nothing.",
    ),
) -> None:
    """Repair objectively-safe continuity issues (stale handoffs, expired
    claims/help requests, expired command leases). Dry-run unless --apply.

    Every action is exactly the fenced helper the affected family's own read
    path already calls opportunistically — never deletes unresolved work
    (an open approval, unread mail, or an un-expired claim/request)."""
    from brains.control.queue_health import apply_repair, plan_repair

    if not apply:
        payload = plan_repair()
        _print_json({"applied": False, **payload})
        return
    payload = apply_repair()
    _print_json({"applied": True, **payload})


@app.command("recovery-policy")
def recovery_policy_cli() -> None:
    """Report the declared recovery policy (BL-P1-09): scope, schedule,
    retention, encryption expectation/owner, RTO/RPO, offsite owner/location,
    and restore-drill requirement — redacted, with completeness and a
    migration/backup compatibility precheck. Never claims backups are
    "managed" unless every mandatory field is configured."""
    from brains.control.recovery_policy import recovery_readiness

    _print_json(recovery_readiness())


# Apply the shipped capability boundary only after every decorator has run.
# Keeping implementation functions importable preserves readers/migrations for
# historical stores without leaving a command-name activation path.
from brains.capabilities import WITHDRAWN_CLI_COMMANDS, WITHDRAWN_CLI_GROUPS  # noqa: E402

app.registered_commands[:] = [
    command for command in app.registered_commands if command.name not in WITHDRAWN_CLI_COMMANDS
]
app.registered_groups[:] = [
    group for group in app.registered_groups if group.name not in WITHDRAWN_CLI_GROUPS
]
