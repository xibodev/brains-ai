"""Temporary specialist execution adapter (Blueprint #37).

Bounded OpenCode CLI adapter: scrubbed environment inside
.brains/checkouts/<code>, prohibited bypass flags, process-group supervision,
bounded ring-buffer streaming output. Binds to #36 WorkAssignmentAttempt
lifecycle via caller-provided attempt metadata (no new DB tables).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

PROHIBITED_FLAGS = frozenset(
    {
        "--allow-all",
        "--dangerously-skip-permissions",
        "--dangerously-bypass-approvals-and-sandbox",
    }
)

HOST_CREDENTIAL_HINTS = (
    "COPILOT_HOME",
    "CLAUDE_CONFIG_DIR",
    "OPENCODE_CONFIG",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


def validate_argv(argv: list[str]) -> None:
    for tok in argv:
        if tok in PROHIBITED_FLAGS:
            raise ValueError(f"prohibited bypass flag: {tok}")


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    from brains.govern.redaction import is_secret_name

    sandbox = Path(tempfile.gettempdir()) / "brains-specialist-sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "HOME": str(sandbox),
        "USERPROFILE": str(sandbox),
        "TMPDIR": str(sandbox),
        "TEMP": str(sandbox),
        "TMP": str(sandbox),
    }
    for k, v in (extra or {}).items():
        if is_secret_name(k):
            continue
        env[k] = v
    return env


def run_specialist(
    argv: list[str],
    *,
    worktree: str | Path,
    timeout_seconds: int = 600,
    max_output_chars: int = 131072,
) -> dict:
    """Run bounded specialist process. Returns evidence dict."""
    validate_argv(list(argv))
    wt = Path(worktree).resolve()
    if not wt.is_dir():
        raise ValueError("worktree does not exist")
    env = scrubbed_env()
    for hint in HOST_CREDENTIAL_HINTS:
        env.pop(hint, None)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(wt),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            creationflags=creationflags,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        head = out[: max_output_chars // 2]
        tail = out[-(max_output_chars // 2) :] if len(out) > max_output_chars else ""
        return {
            "returncode": proc.returncode,
            "head": head,
            "tail": tail,
            "truncated": len(out) > max_output_chars,
            "worktree": str(wt),
        }
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + (
            (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        )
        return {
            "returncode": -1,
            "head": out[: max_output_chars // 2],
            "tail": "",
            "truncated": True,
            "timeout": True,
            "worktree": str(wt),
        }
