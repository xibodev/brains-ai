"""Session runner + PATH-shim installer for gated agent CLI execution.

``install_shims`` writes a tiny wrapper script for each gated binary into a shim
directory; each wrapper re-invokes ``python -m brains.exec.gate <binary> "$@"``
so the action passes through :mod:`brains.exec.gate`. ``run_session`` builds the
gated environment (shim dir first on PATH), records a brains agent-session, and
launches the agent CLI through :mod:`brains.exec.guard` - so the launch itself
is a governed, recorded action rather than a raw ``subprocess`` call.

Shims are written in the host's own script dialect: ``#!/bin/sh`` wrappers on
POSIX and ``.cmd`` wrappers on Windows, because ``PATHEXT`` resolution is what
makes a Windows shim intercept ``git`` at all.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

# Binaries the executor shims onto PATH. Mirrors the gate's classifier; a binary
# absent from the box is simply skipped.
GATED_BINARIES = [
    "git",
    "gh",
    "aws",
    "az",
    "gcloud",
    "vercel",
    "netlify",
    "fly",
    "flyctl",
    "heroku",
    "terraform",
    "pulumi",
    "kubectl",
    "helm",
    "doctl",
    "docker",
    "npm",
    "pnpm",
    "yarn",
    "twine",
    "ssh",
    "scp",
    "rsync",
    "sftp",
    "curl",
    "wget",
]

_POSIX_SHIM = """#!/bin/sh
exec {python} -m brains.exec.gate {binary} "$@"
"""

_WINDOWS_SHIM = """@echo off
"{python}" -m brains.exec.gate {binary} %*
"""


def install_shims(
    shim_dir: str | os.PathLike[str],
    binaries: list[str] | None = None,
    python: str | None = None,
    style: str | None = None,
) -> list[str]:
    """Write gate shims for ``binaries`` into ``shim_dir``. Returns names written.

    Only binaries that actually resolve on the current PATH are shimmed (so we
    don't shadow a missing tool with a broken wrapper). ``style`` forces
    ``posix`` or ``windows`` output; it defaults to the host.
    """
    from brains.exec.guard import resolve_executable

    shim_path = Path(shim_dir)
    shim_path.mkdir(parents=True, exist_ok=True)
    py = python or sys.executable
    windows = (style or ("windows" if os.name == "nt" else "posix")) == "windows"
    written: list[str] = []
    for name in binaries or GATED_BINARIES:
        # Resolve against the *real* PATH (excluding the shim dir itself).
        if resolve_executable(name, exclude_dir=str(shim_path)) is None:
            continue
        if windows:
            script = shim_path / f"{name}.cmd"
            script.write_text(_WINDOWS_SHIM.format(python=py, binary=name), encoding="utf-8")
        else:
            script = shim_path / name
            script.write_text(_POSIX_SHIM.format(python=py, binary=name), encoding="utf-8")
            script.chmod(0o755)
        written.append(name)
    return written


def build_gated_env(
    shim_dir: str | os.PathLike[str],
    workspace_path: str,
    session_id: str | None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return an env with the shim dir first on PATH + the gate's context vars."""
    env = dict(base_env if base_env is not None else os.environ)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    env["BRAINS_GATE_SHIM_DIR"] = str(shim_dir)
    env["BRAINS_GATE_WORKSPACE"] = workspace_path
    if session_id:
        env["BRAINS_GATE_SESSION"] = session_id
    return env


def run_session(
    tool_argv: list[str],
    workspace_path: str,
    prompt: str | None = None,
    *,
    shim_dir: str | os.PathLike[str] | None = None,
    orient_query: str | None = None,
    tool: str = "copilot",
    operator: str | None = None,
    base_env: dict[str, str] | None = None,
    timeout: float | None = None,
    session_id: str | None = None,
    runtime_id: int | None = None,
    on_output: Callable[[str], None] | None = None,
) -> dict:
    """Run a gated agent CLI session.

    ``tool_argv`` is the full command (e.g. ``["copilot","-p","-","--allow-all"]``);
    ``prompt`` (if given) is fed on stdin, prefixed with any Skills attached to
    the Session's Persona/Project (BL-P1-08, deduplicated with provenance) and
    then a brains orientation block for ``orient_query`` (capability-aware
    speedup). Outward actions the agent attempts are gated via the installed
    shims. Records a brains agent-session for attribution + audit. Returns
    ``{session_id, returncode, shimmed, gated}``.

    ``session_id`` binds the launch to a Session row that already exists - the
    row the hub opened for a claimed assignment. Passing it is what stops the
    hub and the Runtime keeping two different Sessions for one process:
    without it the runner starts (and ends) a *local* Session the hub has
    never heard of, while the hub's own row stays ``running`` forever
    (BL-P0-05). The caller that supplied the id owns its terminal state.

    While the agent runs, the process handle is registered with
    :mod:`brains.exec.session_channel`, so an operator stop reaches exactly
    this process and nothing else. ``runtime_id`` records *which* Runtime the
    launch belongs to, which is what lets a box hosting several Runtimes
    report each one only the Sessions it holds. Where the tool declares a
    durable input channel the launch keeps stdin open and streams output,
    which is what makes a queued message actually reach the agent; for every
    tool that does not, the queue settles a message as ``unsupported`` rather
    than pretending.
    """
    from brains.exec import session_channel

    workspace_path = os.path.abspath(workspace_path)
    shim_dir = Path(shim_dir) if shim_dir else Path(workspace_path) / ".brains-gate-bin"
    shimmed = install_shims(shim_dir, python=sys.executable)

    owns_session = session_id is None
    if owns_session:
        try:
            from brains.control.sessions import start_session

            started = start_session(
                workspace_path,
                tool=tool,
                operator=operator,
                lease_session=False,
            )
            session_id = started.get("session_id")
        except Exception:
            session_id = None

    argv_prompt_index: int | None = None
    effective_prompt = prompt
    if tool == "copilot":
        with contextlib.suppress(ValueError, IndexError):
            argv_prompt_index = tool_argv.index("-p") + 1
            if effective_prompt is None:
                effective_prompt = tool_argv[argv_prompt_index]

    # F10 (BL-P1-08): Skills attached to this Session's Persona/Project enter
    # the agent's actual prompt here — the launch path every spawned Session
    # goes through — rather than only a ``build_welcome`` API response a
    # remote-spawned agent never reads. Prepended before the orientation block
    # so identity/capability context reads first; skipped (never a fabricated
    # block) when nothing is attached.
    if effective_prompt is not None and session_id:
        try:
            from brains.control.skills import (
                render_skill_context_block,
                resolve_context_for_session,
            )

            skill_block = render_skill_context_block(resolve_context_for_session(session_id))
            if skill_block and not skill_block.startswith("<!--"):
                effective_prompt = f"{skill_block}\n\n{effective_prompt}"
        except Exception:
            pass

    if effective_prompt is not None and orient_query:
        try:
            from brains.context.semantic import build_orientation_block

            block = build_orientation_block(workspace_path, orient_query)
            if block and not block.startswith("<!--"):
                effective_prompt = f"{block}\n\n{effective_prompt}"
        except Exception:
            pass

    if argv_prompt_index is not None and effective_prompt is not None:
        tool_argv = list(tool_argv)
        tool_argv[argv_prompt_index] = effective_prompt
        prompt = None
    else:
        prompt = effective_prompt

    env = build_gated_env(shim_dir, workspace_path, session_id, base_env=base_env)

    def _observe(process) -> None:
        if session_id:
            session_channel.register(
                session_id, process, tool=tool, stdin_open=False, runtime_id=runtime_id
            )

    try:
        if session_channel.supports_message(tool):
            launched, returncode = _run_interactive(
                tool_argv,
                workspace_path,
                prompt,
                tool=tool,
                operator=operator,
                session_id=session_id,
                runtime_id=runtime_id,
                env=env,
                on_output=on_output,
            )
        else:
            from brains.exec.guard import run as guarded_run

            launched = guarded_run(
                tool_argv,
                actor=operator or "exec-runner",
                action="exec.agent_session",
                workspace_path=workspace_path,
                session_id=session_id,
                cwd=workspace_path,
                env=env,
                input_text=prompt,
                timeout=timeout,
                process_observer=_observe,
            )
            # A refused launch has no exit status of its own; report the gate's
            # denial code rather than a null the caller would read as "ran,
            # told us nothing".
            returncode = launched.returncode if launched.returncode is not None else 13
    finally:
        if session_id:
            session_channel.unregister(session_id)
    if session_id and owns_session:
        try:
            from brains.control.sessions import end_session

            end_session(
                session_id,
                summary=f"gated session ({tool}) rc={returncode}",
            )
        except Exception:
            pass
    return {
        "session_id": session_id,
        "returncode": returncode,
        "allowed": launched.allowed,
        "reason": launched.reason,
        "action_id": launched.action_id,
        "shimmed": shimmed,
        "gated_binaries": GATED_BINARIES,
    }


def _run_interactive(
    tool_argv: list[str],
    workspace_path: str,
    prompt: str | None,
    *,
    tool: str,
    operator: str | None,
    session_id: str | None,
    runtime_id: int | None = None,
    env: dict[str, str],
    on_output: Callable[[str], None] | None,
) -> tuple[Any, int]:
    """Launch a tool that keeps a durable input channel, and stream it.

    Only reached for a tool declared interactive by
    :mod:`brains.exec.session_channel`. stdin stays open for the life of the
    Session, which is precisely what a queued operator message is written to;
    the declaration and this launch path are changed together, so the
    capability the console shows is the capability the process has.
    """
    import subprocess as _subprocess

    from brains.exec import session_channel
    from brains.exec.guard import spawn as guarded_spawn

    launched = guarded_spawn(
        tool_argv,
        actor=operator or "exec-runner",
        action="exec.agent_session",
        workspace_path=workspace_path,
        session_id=session_id,
        cwd=workspace_path,
        env=env,
        stdin=_subprocess.PIPE,
        stdout=_subprocess.PIPE,
        stderr=_subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if not launched.allowed or launched.process is None:
        return launched, 13
    process = launched.process
    if session_id:
        session_channel.register(
            session_id, process, tool=tool, stdin_open=True, runtime_id=runtime_id
        )
    if prompt is not None and process.stdin is not None:
        with contextlib.suppress(OSError, ValueError):
            process.stdin.write(prompt if prompt.endswith("\n") else f"{prompt}\n")
            process.stdin.flush()
    if process.stdout is not None:
        for line in process.stdout:
            if on_output is not None:
                with contextlib.suppress(Exception):
                    on_output(line)
    return launched, process.wait()


def _build_tool_argv(tool: str, prompt: str, model: str | None) -> tuple[list[str], str | None]:
    """Map a tool name to its non-interactive argv + the stdin feed (if any)."""
    if tool == "copilot":
        argv = ["copilot", "-p", prompt, "--allow-all"]
        if model:
            argv += ["--model", model]
        return argv, None
    if tool == "claude":
        argv = [
            "claude",
            "-p",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
        ]
        if model:
            argv += ["--model", model]
        return argv, prompt
    if tool == "codex":
        argv = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if model:
            argv += ["-m", model]
        return argv, prompt
    raise ValueError("tool must be copilot|claude|codex")


def start_streamed_session(
    tool: str,
    prompt: str,
    workspace_path: str,
    *,
    model: str | None = None,
    orient_query: str | None = None,
    operator: str | None = None,
) -> str:
    """Launch a gated agent session in a BACKGROUND thread, streaming its output
    to the file-backed exec store so the dashboard can tail it live. Returns the
    ``exec_id`` immediately. Outward actions are gated exactly as in ``run_session``.
    """
    import subprocess
    import threading

    from brains.exec import store
    from brains.exec.guard import spawn as guarded_spawn

    workspace_path = os.path.abspath(workspace_path)
    meta = store.create(
        tool=tool, model=model, workspace=workspace_path, prompt=prompt, operator=operator
    )
    exec_id = meta.exec_id

    def _run() -> None:
        import contextlib

        shim_dir = Path(workspace_path) / ".brains-gate-bin"
        with contextlib.suppress(Exception):
            install_shims(shim_dir, python=sys.executable)
        session_id = None
        try:
            from brains.control.sessions import start_session

            started = start_session(
                workspace_path,
                tool=tool,
                operator=operator,
                lease_session=False,
            )
            session_id = started.get("session_id")
        except Exception:
            session_id = None

        eff_prompt = prompt
        # Decide the orientation query: explicit wins; otherwise auto-inject only
        # for weak/cheap models (capability-aware, per the model-ladder study).
        eff_orient = orient_query
        if eff_orient is None:
            from brains.exec.orient_policy import should_orient

            if should_orient(model):
                eff_orient = prompt[:200]
        if eff_orient:
            try:
                from brains.context.semantic import build_orientation_block

                block = build_orientation_block(workspace_path, eff_orient)
                if block and not block.startswith("<!--"):
                    eff_prompt = f"{block}\n\n{prompt}"
            except Exception:
                pass

        argv, feed = _build_tool_argv(tool, eff_prompt, model)
        env = build_gated_env(shim_dir, workspace_path, session_id, base_env=None)
        try:
            launched = guarded_spawn(
                argv,
                actor=operator or "exec-runner",
                action="exec.agent_session",
                workspace_path=workspace_path,
                session_id=session_id,
                cwd=workspace_path,
                env=env,
                stdin=subprocess.PIPE if feed is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if not launched.allowed or launched.process is None:
                store.append_output(
                    exec_id,
                    f"\n[brains exec] refused: {launched.reason or launched.status}\n",
                )
                store.set_status(exec_id, "failed", returncode=13)
                return
            proc = launched.process
            store.set_status(exec_id, "running", pid=proc.pid)
            if session_id:
                # The hub process owns this handle, so an operator stop for
                # this Session can be delivered here rather than queued for a
                # Runtime that never launched it (BL-P0-05).
                from brains.exec import session_channel

                session_channel.register(session_id, proc, tool=tool, stdin_open=False)
            if feed is not None and proc.stdin:
                proc.stdin.write(feed)
                proc.stdin.close()
            assert proc.stdout is not None
            for line in proc.stdout:
                store.append_output(exec_id, line)
            rc = proc.wait()
            store.set_status(exec_id, "done" if rc == 0 else "failed", returncode=rc)
        except Exception as exc:  # pragma: no cover - launch failure path
            store.append_output(exec_id, f"\n[brains exec] launch error: {exc}\n")
            store.set_status(exec_id, "failed", returncode=-1)
        finally:
            if session_id:
                from brains.exec import session_channel

                session_channel.unregister(session_id)
                try:
                    from brains.control.sessions import end_session

                    end_session(session_id, summary=f"streamed gated session ({tool})")
                except Exception:
                    pass

    threading.Thread(target=_run, name=f"brains-exec-{exec_id}", daemon=True).start()
    return exec_id
