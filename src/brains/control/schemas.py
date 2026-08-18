from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class WorkspaceInfo(BaseModel):
    id: int
    slug: str
    path: str
    status: str
    last_touched_at: datetime | None = None
    last_summary: str | None = None


class SessionInfo(BaseModel):
    id: str
    workspace_slug: str
    tool: str
    started_at: datetime
    ended_at: datetime | None = None
    summary: str | None = None
    active_handoff: dict[str, Any] | None = None


class DecisionRequestInfo(BaseModel):
    code: str
    workspace_slug: str
    title: str
    status: str
    proposed_answer: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class HandoffInfo(BaseModel):
    id: int
    workspace_slug: str
    title: str
    body: str | None = None
    status: str
    set_at: datetime
