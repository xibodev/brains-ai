from pydantic import BaseModel


class PlanRequestInput(BaseModel):
    prompt: str
    workspace_path: str | None = None


class SearchRepoInput(BaseModel):
    query: str | None = None
    q: str | None = None
    repo_path: str = "."
    path: str | None = None
    limit: int = 10


class StateInput(BaseModel):
    workspace_path: str | None = None
    session_id: str | None = None
    limit: int = 50


class SessionStartInput(BaseModel):
    workspace_path: str = "."
    tool: str = "codex"


class SessionEndInput(BaseModel):
    session_id: str
    summary: str = ""


class DecisionRequestInput(BaseModel):
    workspace_path: str = "."
    title: str
    body: str = ""
    proposed_answer: str | None = None
    session_id: str | None = None


class DecisionResolveInput(BaseModel):
    code: str
    chosen: str
    reasoning: str = ""
    status: str = "resolved"


class HandoffInput(BaseModel):
    workspace_path: str = "."
    title: str
    body: str = ""
    session_id: str | None = None


class TaskCreateInput(BaseModel):
    workspace_path: str = "."
    title: str
    body: str = ""
    priority: str = "p2"
    depends_on: str = ""
    tags: str = ""
    session_id: str | None = None


class TaskActionInput(BaseModel):
    task_code: str
    session_id: str
    summary: str = ""
    reason: str = ""


class WorkspaceClaimInput(BaseModel):
    workspace_path: str = "."
    session_id: str
    scope: str = "code"
    duration_minutes: int = 30


class MessageSendInput(BaseModel):
    subject: str
    body: str = ""
    from_session_id: str | None = None
    to_session_id: str | None = None
    workspace_path: str | None = None
    kind: str = "info"


class MessageReadInput(BaseModel):
    session_id: str
    mark_read: bool = True
    include_read: bool = False
    limit: int = 50


class SnapshotInput(BaseModel):
    workspace_path: str = "."
    kind: str
    data: dict | str
    session_id: str | None = None


class PatternProposeInput(BaseModel):
    name: str
    category: str
    description: str
    example: str = ""
    applies_to: str = ""
    session_id: str | None = None


class PatternApproveInput(BaseModel):
    name: str
    approved: bool = True


class PatternListInput(BaseModel):
    category: str | None = None
    status: str = "approved"
    limit: int = 100


class ToolRegisterInput(BaseModel):
    name: str
    display_name: str
    cli_command: str
    spawn_args: str = ""
    capabilities: str = ""
    notes: str = ""
    verify: bool = True


class ToolVerifyInput(BaseModel):
    name: str


class RecurringCreateInput(BaseModel):
    workspace_path: str = "."
    name: str
    title_template: str
    body_template: str = ""
    priority: str = "p2"
    tags: str = ""
    cron_expr: str = "manual"
    session_id: str | None = None


class RecurringListInput(BaseModel):
    workspace_path: str | None = None
    enabled: bool | None = None
    limit: int = 100


class RecurringEnableInput(BaseModel):
    name: str
    enabled: bool = True


class RecurringFireInput(BaseModel):
    name: str
    session_id: str | None = None
