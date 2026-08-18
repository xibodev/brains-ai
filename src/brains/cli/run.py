"""``brains-ai run <tool>`` — launch an LLM CLI tool with brains as
its API gateway.

This is a deliberate, ergonomic switch between the user's default
provider (e.g. real Anthropic/OpenAI) and brains' proxying endpoint.

Adding a new tool requires adding an entry to :data:`TOOLS` that names
the binary + the env vars that point it at an OpenAI-compatible /
Anthropic-compatible endpoint.

The runner builds the env, validates the binary is on PATH, then uses
``os.execvp`` so the tool inherits TTY (Ctrl+C works natively, no
double signal handling, no orphaned subprocess).
"""

from __future__ import annotations

import os
import shutil
from typing import NoReturn

import typer


class ToolSpec:
    """How to point one LLM CLI at brains' gateway."""

    def __init__(
        self,
        binary: str,
        env: dict[str, str],
        *,
        description: str = "",
    ) -> None:
        self.binary = binary
        self.env = env
        self.description = description


def _default_gateway() -> str:
    """OpenAI-compatible base URL the gateway listens on."""
    return os.environ.get("BRAINS_GATEWAY_URL", "http://127.0.0.1:8787/v1")


def _default_api_key() -> str:
    """Admin key the gateway expects. Lazy import to avoid pulling the
    whole brains stack when this module is imported by the shim."""
    explicit = os.environ.get("BRAINS_API_KEY")
    if explicit:
        return explicit
    from brains.api.admin_key import ensure_admin_key

    key, _ = ensure_admin_key(print_banner=False)
    return key


def _tools() -> dict[str, ToolSpec]:
    """Built fresh per call so env-derived defaults pick up changes."""
    gateway = _default_gateway()
    # Anthropic CLI uses /v1, Copilot CLI uses the OpenAI-compatible base.
    return {
        "claude": ToolSpec(
            binary="claude",
            env={
                "ANTHROPIC_BASE_URL": gateway.rstrip("/").removesuffix("/v1"),
                "ANTHROPIC_AUTH_TOKEN": _default_api_key(),
            },
            description="Anthropic Claude Code CLI",
        ),
        "copilot": ToolSpec(
            binary="copilot",
            env={
                "OPENAI_BASE_URL": gateway,
                "OPENAI_API_KEY": _default_api_key(),
            },
            description="GitHub Copilot CLI",
        ),
    }


def _resolve_tool(name: str) -> ToolSpec:
    tools = _tools()
    if name not in tools:
        valid = ", ".join(sorted(tools))
        raise typer.BadParameter(f"Unknown tool {name!r}. Valid: {valid}")
    return tools[name]


def _exec_tool(spec: ToolSpec, tool_args: list[str]) -> NoReturn:
    """Replace the current process with the tool. Never returns on success."""
    bin_path = shutil.which(spec.binary)
    if not bin_path:
        typer.secho(
            f"⨯ `{spec.binary}` not found on PATH. Install it first, then re-run.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(127)

    # Preserve the caller's env; brains-specific vars override.
    merged_env = {**os.environ, **spec.env}
    typer.secho(
        f"→ launching `{spec.binary} {' '.join(tool_args)}` against "
        f"brains-ai gateway at {_default_gateway()}",
        err=True,
        fg=typer.colors.CYAN,
    )
    # execvpe replaces the current process; Ctrl+C goes straight to the
    # tool, no subprocess wrapper to fight with. Works on POSIX natively;
    # on Windows it spawns a new process and terminates the parent, which
    # is functionally equivalent for our use case (interactive REPL).
    os.execvpe(bin_path, [bin_path, *tool_args], merged_env)
    # Defensive: execvpe never returns on success.
    raise RuntimeError("execvpe returned unexpectedly")


def run_tool_cli(
    ctx: typer.Context,
    tool: str = typer.Argument(
        ...,
        help="LLM CLI to launch with the brains gateway. One of: claude, copilot.",
    ),
) -> None:
    """Launch an LLM CLI with brains as its API gateway.

    Examples:

      brains-ai run claude                 # interactive claude session via brains
      brains-ai run copilot                # interactive copilot session via brains
      brains-ai run claude --resume        # forward `--resume` to claude

    Every argument after the tool name is forwarded verbatim — unknown
    flags (``--resume``, ``--model``, etc.) are not consumed by brains.
    """
    spec = _resolve_tool(tool)
    # ctx.args carries everything after the tool name verbatim when the
    # command is registered with allow_extra_args=True.
    _exec_tool(spec, list(ctx.args))
