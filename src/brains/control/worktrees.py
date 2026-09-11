"""Isolated git checkout and worktree lifecycle management (Issue #36 AC3).

Manages ephemeral git worktrees under `.brains/checkouts/<assignment-code>` to provide
filesystem and branch isolation for parallel work assignments, with automatic tracking
and stale worktree reclamation.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from brains.control.common import utc_now

ASSIGNMENT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class WorktreeError(RuntimeError):
    """Raised when git worktree creation, inspection, or cleanup fails."""


def _run_git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def get_repo_root(path: Path | str | None = None) -> Path:
    start = Path(path or ".").resolve()
    proc = _run_git(start, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0:
        raise WorktreeError(f"not inside a git repository: {start}")
    return Path(proc.stdout.strip()).resolve(strict=True)


def checkouts_dir(repo_root: Path | str) -> Path:
    root = Path(repo_root).resolve(strict=True)
    target = root / ".brains" / "checkouts"
    target.mkdir(parents=True, exist_ok=True)
    return target


def create_worktree(
    repo_root: Path | str,
    assignment_code: str,
    *,
    base_ref: str = "HEAD",
    branch_name: str | None = None,
) -> dict[str, Any]:
    """Create an isolated git worktree and branch for an assignment."""
    if not ASSIGNMENT_CODE_RE.fullmatch(assignment_code):
        raise WorktreeError(f"invalid assignment code for worktree: {assignment_code}")

    root = Path(repo_root).resolve(strict=True)
    base = checkouts_dir(root)
    worktree_path = (base / assignment_code).resolve()
    branch = branch_name or f"brains/work/{assignment_code}"
    metadata_file = worktree_path / ".brains-worktree.json"

    # If worktree already exists and is registered, return existing metadata
    if worktree_path.is_dir() and metadata_file.is_file():
        try:
            existing = json.loads(metadata_file.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                return {
                    **existing,
                    "reused": True,
                    "status": "active",
                }
        except (OSError, json.JSONDecodeError):
            pass

    # Check if branch exists
    branch_exists = (
        _run_git(root, "rev-parse", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
    )

    if branch_exists:
        _run_git(root, "worktree", "add", "--force", str(worktree_path), branch)
    else:
        _run_git(root, "worktree", "add", "-b", branch, str(worktree_path), base_ref)

    now_iso = utc_now().isoformat()
    record = {
        "assignment_code": assignment_code,
        "worktree_path": str(worktree_path),
        "branch": branch,
        "base_ref": base_ref,
        "created_at": now_iso,
        "status": "active",
        "reused": False,
    }
    try:
        metadata_file.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise WorktreeError(f"failed to write worktree metadata: {exc}") from exc

    return record


def cleanup_worktree(
    repo_root: Path | str,
    assignment_code: str,
    *,
    force: bool = True,
    delete_branch: bool = False,
) -> dict[str, Any]:
    """Remove an isolated git worktree and optionally delete its branch."""
    if not ASSIGNMENT_CODE_RE.fullmatch(assignment_code):
        raise WorktreeError(f"invalid assignment code: {assignment_code}")

    root = Path(repo_root).resolve(strict=True)
    worktree_path = (checkouts_dir(root) / assignment_code).resolve()
    branch = f"brains/work/{assignment_code}"

    # Remove via git worktree remove
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))

    _run_git(root, *args, check=False)
    _run_git(root, "worktree", "prune", check=False)

    # Clean leftover directory if git worktree remove left any files
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)

    branch_deleted = False
    if delete_branch:
        del_proc = _run_git(root, "branch", "-D", branch, check=False)
        branch_deleted = del_proc.returncode == 0

    return {
        "cleaned": True,
        "assignment_code": assignment_code,
        "worktree_path": str(worktree_path),
        "branch_deleted": branch_deleted,
    }


def list_managed_worktrees(repo_root: Path | str) -> list[dict[str, Any]]:
    """List all active worktrees managed by Brains under .brains/checkouts."""
    root = Path(repo_root).resolve(strict=True)
    base = checkouts_dir(root)
    proc = _run_git(root, "worktree", "list", "--porcelain", check=True)

    records: list[dict[str, Any]] = []
    current: dict[str, str] = {}

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            if current and "worktree" in current:
                wt_path = Path(current["worktree"]).resolve()
                try:
                    if wt_path.is_relative_to(base) and wt_path != base:
                        assignment_code = wt_path.name
                        meta_file = wt_path / ".brains-worktree.json"
                        created_at = None
                        if meta_file.is_file():
                            try:
                                data = json.loads(meta_file.read_text(encoding="utf-8"))
                                created_at = data.get("created_at")
                            except Exception:
                                pass
                        records.append(
                            {
                                "assignment_code": assignment_code,
                                "worktree_path": str(wt_path),
                                "branch": current.get("branch", "").replace("refs/heads/", ""),
                                "head": current.get("HEAD", ""),
                                "created_at": created_at,
                            }
                        )
                except ValueError:
                    pass
            current = {}
            continue

        parts = line.split(" ", 1)
        if len(parts) == 2:
            current[parts[0]] = parts[1]
        elif len(parts) == 1:
            current[parts[0]] = ""

    if current and "worktree" in current:
        wt_path = Path(current["worktree"]).resolve()
        try:
            if wt_path.is_relative_to(base) and wt_path != base:
                assignment_code = wt_path.name
                meta_file = wt_path / ".brains-worktree.json"
                created_at = None
                if meta_file.is_file():
                    try:
                        data = json.loads(meta_file.read_text(encoding="utf-8"))
                        created_at = data.get("created_at")
                    except Exception:
                        pass
                records.append(
                    {
                        "assignment_code": assignment_code,
                        "worktree_path": str(wt_path),
                        "branch": current.get("branch", "").replace("refs/heads/", ""),
                        "head": current.get("HEAD", ""),
                        "created_at": created_at,
                    }
                )
        except ValueError:
            pass

    return records


def prune_stale_worktrees(
    repo_root: Path | str,
    *,
    db_session=None,
    max_age_hours: int = 24,
    force: bool = True,
    delete_branches: bool = False,
) -> list[dict[str, Any]]:
    """Inspect managed worktrees and prune those belonging to terminal or stale assignments."""
    from brains.storage.models import WorkAssignment

    root = Path(repo_root).resolve(strict=True)
    managed = list_managed_worktrees(root)
    pruned: list[dict[str, Any]] = []
    now = utc_now()
    age_cutoff = now - timedelta(hours=max_age_hours)

    for item in managed:
        code = item["assignment_code"]
        reason = None

        if db_session is not None:
            assignment = db_session.get(WorkAssignment, code)
            if assignment is not None:
                if assignment.status in ("completed", "cancelled", "failed"):
                    reason = f"assignment_{assignment.status}"
                elif assignment.deadline_at and assignment.deadline_at.replace(tzinfo=UTC) < now:
                    reason = "assignment_deadline_exceeded"
            else:
                reason = "orphan_worktree_no_assignment"

        if reason is None and item.get("created_at"):
            try:
                created = datetime.fromisoformat(item["created_at"]).astimezone(UTC)
                if created < age_cutoff:
                    reason = "worktree_max_age_exceeded"
            except (ValueError, TypeError):
                pass

        if reason is not None:
            cleanup_res = cleanup_worktree(root, code, force=force, delete_branch=delete_branches)
            pruned.append({**cleanup_res, "reason": reason})

    return pruned


def worktree_instructions(assignment_code: str, worktree_path: str, branch: str) -> str:
    """Return explicit instruction string for an agent operating in an isolated worktree."""
    return (
        f"You have been assigned to work in isolated git worktree: {worktree_path}\n"
        f"Checked out on branch: {branch}\n"
        f"All code edits and testing must take place inside {worktree_path}.\n"
        f"Commit your progress to branch {branch} when finished and submit completion evidence.\n"
        f"Do not edit files in the root worktree. Brains manages worktree cleanup upon assignment completion."
    )
