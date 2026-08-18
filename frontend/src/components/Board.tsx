import type { Issue, IssueStatus } from "../api/types";
import { StatusPill } from "./StatusPill";

export const BOARD_COLUMNS: IssueStatus[] = [
  "open",
  "in_progress",
  "blocked",
  "in_review",
  "done",
];

const COLUMN_LABELS: Record<string, string> = {
  open: "OPEN",
  in_progress: "IN PROGRESS",
  blocked: "BLOCKED",
  in_review: "IN REVIEW",
  done: "DONE",
  cancelled: "CANCELLED",
};

function IssueCard({
  issue,
  onClick,
  onDragStart,
}: {
  issue: Issue;
  onClick: () => void;
  onDragStart: () => void;
}) {
  return (
    <div
      className="issue-card"
      draggable
      onDragStart={onDragStart}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter") onClick();
      }}
    >
      <div className="row spread">
        <span className="code">{issue.code}</span>
        {issue.has_live_session && <span className="dot live" title="live session" />}
      </div>
      <div className="title">{issue.title}</div>
      <div className="row wrap" style={{ gap: 6 }}>
        {issue.priority && <StatusPill label={issue.priority} />}
        {issue.assignee_label && <span className="meta">⌾ {issue.assignee_label}</span>}
      </div>
    </div>
  );
}

// Status-column kanban with native HTML5 drag to change status. `cancelled` is
// never a default column — it's reachable via a filter (DESIGN-SYNTHESIS lock).
export function Board({
  issues,
  columns = BOARD_COLUMNS,
  onOpen,
  onMove,
  compact = false,
}: {
  issues: Issue[];
  columns?: IssueStatus[];
  onOpen: (issue: Issue) => void;
  onMove: (issue: Issue, status: IssueStatus) => void;
  compact?: boolean;
}) {
  let dragged: Issue | null = null;

  return (
    <div className={compact ? "board board-compact" : "board"}>
      {columns.map((col) => {
        const colIssues = issues.filter((i) => i.status === col);
        return (
          <div
            key={col}
            className="board-col"
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => {
              if (dragged && dragged.status !== col) onMove(dragged, col);
              dragged = null;
            }}
          >
            <div className="board-col-head">
              <span className="eyebrow">
                <span>{COLUMN_LABELS[col] ?? col}</span>
              </span>
              <span className="count">{colIssues.length}</span>
            </div>
            <div className="board-col-body">
              {colIssues.map((issue) => (
                <IssueCard
                  key={issue.code}
                  issue={issue}
                  onClick={() => onOpen(issue)}
                  onDragStart={() => {
                    dragged = issue;
                  }}
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
