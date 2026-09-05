"""Experimental feature gate — the loud boundary between mature and not.

Every feature here is real code with real tests, but it does not yet meet
the product's maturity bar (see ``docs/product/PRODUCT_BRIEF.md``):
end-to-end behavior is unproven on a shipped surface, evidence levels are
below E3, or an enforcement boundary is still cooperative. The default
install **hides and refuses** these surfaces; an operator reaches them only
through an explicit environment opt-in, and every refusal names that switch.

Independent gates:

- ``BRAINS_MCP_EXPERIMENTAL=1``  — experimental MCP tools (semantic/graph
  retrieval, session chat delivery) and Autopilot *scheduled auto-fire*.
  Manual fire stays ungated.
- ``BRAINS_UI_LABS=1`` — unfinished execution-model screens in the modern
  console (Personas, Pods, Projects, Issues, Sessions, Runtimes, Automation,
  and onboarding). They stay routable only behind this explicit UI opt-in.

This module is deliberately dependency-free so any layer (MCP server,
supervisor, CLI, FastAPI middleware) can import it cheaply.
"""

from __future__ import annotations

import os

EXPERIMENTAL_ENV = "BRAINS_MCP_EXPERIMENTAL"
GATEWAY_ENV = "BRAINS_EXPERIMENTAL_GATEWAY"
UI_LABS_ENV = "BRAINS_UI_LABS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: MCP tool names excluded from the default advertised/callable surface.
#: Keep in sync with TOOL_REGISTRY in ``brains.mcp.server`` — a name here
#: that no longer exists there is dead weight, and a test pins that.
EXPERIMENTAL_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "search_semantic",
        "graph_build",
        "graph_query",
        "graph_neighbors",
        "graph_path",
        "graph_subsystems",
        "graph_export",
        "session_message",
    }
)

#: Why each gated tool sits behind the flag. One line each — these strings
#: are surfaced verbatim in refusals so an operator never has to guess.
EXPERIMENTAL_TOOL_REASONS: dict[str, str] = {
    "search_semantic": "embeddings need a configured local model; matches are empty without one",
    "graph_build": "code graph is withdrawn; remaining discovery and activation are containment work (BL-P0-09)",
    "graph_query": "code graph is withdrawn; remaining discovery and activation are containment work (BL-P0-09)",
    "graph_neighbors": "code graph is withdrawn; remaining discovery and activation are containment work (BL-P0-09)",
    "graph_path": "code graph is withdrawn; remaining discovery and activation are containment work (BL-P0-09)",
    "graph_subsystems": "code graph is withdrawn; remaining discovery and activation are containment work (BL-P0-09)",
    "graph_export": "code graph is withdrawn; remaining discovery and activation are containment work (BL-P0-09)",
    "session_message": (
        "no shipped agent CLI is launched with an input channel; delivery to "
        "copilot/claude/codex is a durable refusal (AC-F3-05)"
    ),
}


class ExperimentalDisabledError(RuntimeError):
    """Raised when a gated surface is used without its opt-in flag set."""


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


def experimental_enabled() -> bool:
    """True when ``BRAINS_MCP_EXPERIMENTAL`` opts this process into the
    experimental MCP tool surface and scheduled Autopilot auto-fire."""
    return _env_flag(EXPERIMENTAL_ENV)


def gateway_experimental_enabled() -> bool:
    """True when ``BRAINS_EXPERIMENTAL_GATEWAY`` opts this process into the
    model-serving surface (OpenAI/Anthropic-compatible proxy routes and the
    ``brains-ai run`` launcher). The native control-plane API, console, MCP
    and coordination surfaces are unaffected by this gate."""
    return _env_flag(GATEWAY_ENV)


def ui_labs_enabled() -> bool:
    """True when unfinished modern-console execution screens are enabled."""
    return _env_flag(UI_LABS_ENV)


def require_experimental(label: str) -> None:
    """Raise :class:`ExperimentalDisabledError` unless the experimental gate
    is enabled. ``label`` names the surface in the refusal message."""
    if experimental_enabled():
        return
    raise ExperimentalDisabledError(
        f"{label} is experimental and disabled in the normal install. "
        f"Set {EXPERIMENTAL_ENV}=1 to enable it."
    )


#: The opt-in switches and what each unlocks. Single source of truth
#: for docs and operator-facing summaries.
EXPERIMENTAL_GATES: dict[str, str] = {
    EXPERIMENTAL_ENV: (
        "experimental MCP tools (semantic/graph retrieval, session chat "
        "delivery), Autopilot scheduled auto-fire, and their CLI equivalents"
    ),
    GATEWAY_ENV: (
        "the model-serving surface: OpenAI/Anthropic-compatible proxy routes "
        "(/v1/chat/completions, /v1/messages, /v1/responses, /v1/models) and "
        "the `brains-ai run <tool>` launcher. Model access is expected to come "
        "from each CLI's own provider logins instead."
    ),
    UI_LABS_ENV: (
        "unfinished execution-model screens in the modern console: Sessions, "
        "Personas, Pods, Projects, Issues, Runtimes, Automation, and onboarding"
    ),
}
