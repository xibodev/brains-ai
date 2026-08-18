import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { formatApiError } from "../api/client";
import type { Issue, IssueDispatchPlan, IssueEvidence, IssueStatus } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { useDock } from "../store/DockContext";
import { useAsync } from "../store/useAsync";
import { useTopic } from "../realtime/useRealtime";
import { ScreenHead } from "./ScreenHead";
import { Board, BOARD_COLUMNS } from "../components/Board";
import { Drawer } from "../components/Drawer";
import { StatusPill } from "../components/StatusPill";
import { SoftCard } from "../components/SoftCard";
import { Loading, EmptyState } from "../components/EmptyState";
import { TextField, TextArea, Select } from "../components/Field";
import { useToast } from "../components/Toast";
import { blockedCopy } from "../components/blockedReason";

// Issues board (WS4 §3.6). Kanban with drag-to-transition; cancelled hidden
// behind a filter (DESIGN-SYNTHESIS lock); detail opens in a slide-over drawer.
//
// Issues are created from structured fields only (AC-F4-07): Brains ships no
// natural-language Issue parser, so nothing here turns a sentence into work
// nobody reviewed.
export function Issues() {
  const { activeOrg } = useOrg();
  const { openInChat } = useDock();
  const { toast } = useToast();
  const navigate = useNavigate();
  const { code: routeCode } = useParams();

  const [priorityFilter, setPriorityFilter] = useState("");
  const [showCancelled, setShowCancelled] = useState(false);
  const [open, setOpen] = useState<Issue | null>(null);
  const [creating, setCreating] = useState(false);

  const state = useAsync<Issue[]>(
    () =>
      activeOrg
        ? api.listIssues({
            org_id: String(activeOrg.id),
            ...(priorityFilter ? { priority: priorityFilter } : {}),
          })
        : Promise.resolve([]),
    [activeOrg?.id, priorityFilter],
  );

  useTopic(activeOrg ? `org/${activeOrg.id}/issues` : null, () => state.refetch());

  // Deep link: /app/issues/:code opens that issue, and an unknown code says so
  // instead of quietly showing the board (AC-F0-05).
  const [deepLinkMissing, setDeepLinkMissing] = useState(false);
  useEffect(() => {
    if (!routeCode || state.data === undefined) {
      setDeepLinkMissing(false);
      return;
    }
    const match = (state.data ?? []).find((issue) => issue.code === routeCode);
    setDeepLinkMissing(!match);
    if (match) setOpen(match);
  }, [routeCode, state.data]);

  const closeDetail = () => {
    setOpen(null);
    if (routeCode) navigate("/issues");
  };

  const move = async (issue: Issue, status: IssueStatus) => {
    // optimistic
    state.setData((prev) =>
      (prev ?? []).map((i) => (i.code === issue.code ? { ...i, status } : i)),
    );
    try {
      await api.transitionIssue(issue.code, status);
      toast(`${issue.code} → ${status}`);
    } catch (e) {
      toast(formatApiError("Move", e));
      state.refetch();
    }
  };

  const columns: IssueStatus[] = showCancelled
    ? [...BOARD_COLUMNS, "cancelled"]
    : BOARD_COLUMNS;

  return (
    <div>
      <ScreenHead
        eyebrow="Issues"
        title="Operational board"
        actions={
          <button className="btn primary" onClick={() => setCreating(true)}>
            + New issue
          </button>
        }
      />

      <div className="row wrap" style={{ marginBottom: 16 }}>
        <select
          className="btn"
          value={priorityFilter}
          onChange={(e) => setPriorityFilter(e.target.value)}
        >
          <option value="">All priorities</option>
          <option value="p0">p0</option>
          <option value="p1">p1</option>
          <option value="p2">p2</option>
          <option value="p3">p3</option>
        </select>
        <button
          className={`tab ${showCancelled ? "active" : ""}`}
          onClick={() => setShowCancelled((s) => !s)}
        >
          {showCancelled ? "Hide cancelled" : "Show cancelled"}
        </button>
      </div>

      {deepLinkMissing && (
        <SoftCard>
          <div className="meta" data-testid="issue-not-found">
            No issue named <strong>{routeCode}</strong> in this org.
          </div>
        </SoftCard>
      )}

      {state.loading && state.data === undefined ? (
        <Loading />
      ) : state.error ? (
        <EmptyState
          title="Issues could not be loaded"
          body={formatApiError("Load issues", state.error)}
          action={
            <button className="btn" onClick={() => state.refetch()}>
              Retry
            </button>
          }
        />
      ) : (state.data ?? []).length === 0 ? (
        <EmptyState
          title="No issues yet"
          body="Create the first unit of work and assign it to a persona or pod."
          action={
            <button className="btn primary" onClick={() => setCreating(true)}>
              + New issue
            </button>
          }
        />
      ) : (
        <Board
          issues={state.data ?? []}
          columns={columns}
          onOpen={(i) => navigate(`/issues/${i.code}`)}
          onMove={move}
        />
      )}

      <Drawer open={!!open} onClose={closeDetail}>
        {open && (
          <IssueDetail
            issue={open}
            onChanged={() => state.refetch()}
            onOpenChat={openInChat}
          />
        )}
      </Drawer>

      {creating && (
        <CreateIssue
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            state.refetch();
          }}
        />
      )}
    </div>
  );
}

function IssueDetail({
  issue,
  onChanged,
  onOpenChat,
}: {
  issue: Issue;
  onChanged: () => void;
  onOpenChat: (id: string | number) => void;
}) {
  const { activeOrg } = useOrg();
  const { toast } = useToast();
  const evidence = useAsync<IssueEvidence | null>(
    () => api.issueEvidence(issue.code).catch(() => null),
    [issue.code],
  );
  const plan = useAsync<IssueDispatchPlan | null>(
    () => api.issueDispatchPlan(issue.code).catch(() => null),
    [issue.code],
  );
  const comments = useAsync(() => api.listIssueComments(issue.code).catch(() => []), [issue.code]);
  const [draft, setDraft] = useState("");
  const [posting, setPosting] = useState(false);

  // Assignee picker data
  const members = useAsync(
    () => (activeOrg ? api.listOrgMembers(activeOrg.slug).catch(() => []) : Promise.resolve([])),
    [activeOrg?.slug],
  );
  const personas = useAsync(
    () => (activeOrg ? api.listPersonas(activeOrg.slug).catch(() => []) : Promise.resolve([])),
    [activeOrg?.slug],
  );
  const pods = useAsync(
    () => (activeOrg ? api.listPods(activeOrg.slug).catch(() => []) : Promise.resolve([])),
    [activeOrg?.slug],
  );

  const [assignedValue, setAssignedValue] = useState(
    issue.assignee_persona_id
      ? `persona:${issue.assignee_persona_id}`
      : issue.assignee_pod_id
        ? `pod:${issue.assignee_pod_id}`
        : issue.assignee_operator_id
          ? `member:${issue.assignee_operator_id}`
          : "",
  );

  const refreshExecution = () => {
    evidence.refetch();
    plan.refetch();
  };

  const assign = async (encoded: string) => {
    if (!encoded) return;
    const sep = encoded.indexOf(":");
    const kind = encoded.slice(0, sep);
    const id = encoded.slice(sep + 1);
    let body: Record<string, string>;
    if (kind === "persona") body = { persona_id: id };
    else if (kind === "pod") body = { pod_id: id };
    else body = { operator_id: id };
    try {
      await api.assignIssue(issue.code, body);
      setAssignedValue(encoded);
      toast("Assigned");
      refreshExecution();
      onChanged();
    } catch (e) {
      toast(formatApiError("Assign", e));
    }
  };

  const dispatch = async () => {
    try {
      const result = await api.dispatchIssue(issue.code);
      toast(result.duplicate ? "Already dispatched — showing the running session" : "Dispatched");
      refreshExecution();
      onChanged();
    } catch (e) {
      toast(formatApiError("Dispatch", e));
    }
  };

  const postComment = async () => {
    if (!draft.trim()) return;
    setPosting(true);
    try {
      await api.addIssueComment(issue.code, { body: draft.trim() });
      setDraft("");
      comments.refetch();
    } catch (e) {
      toast(formatApiError("Comment", e));
    } finally {
      setPosting(false);
    }
  };

  const transition = async (status: IssueStatus) => {
    try {
      await api.transitionIssue(issue.code, status);
      toast(`${issue.code} → ${status}`);
      onChanged();
    } catch (e) {
      toast(formatApiError(`Transition ${issue.code}`, e));
    }
  };

  return (
    <div>
      <div className="eyebrow"><span>{issue.code}</span></div>
      <h2 style={{ margin: "8px 0 12px" }}>{issue.title}</h2>
      <div className="row wrap" style={{ marginBottom: 16 }}>
        <StatusPill label={issue.status} />
        {issue.priority && <StatusPill label={issue.priority} />}
        {issue.assignee_label && <span className="meta">⌾ {issue.assignee_label}</span>}
      </div>
      {issue.body && (
        <SoftCard>
          <div style={{ whiteSpace: "pre-wrap" }}>{issue.body}</div>
        </SoftCard>
      )}

      <div className="eyebrow" style={{ margin: "20px 0 8px" }}><span>Assignee</span></div>
      <label className="field">
        <span>Assign to…</span>
        <select value={assignedValue} onChange={(e) => void assign(e.target.value)}>
          <option value="">— unassigned —</option>
          <optgroup label="Members">
            {(members.data ?? []).map((m) => (
              <option key={String(m.operator)} value={`member:${m.operator}`}>
                {m.name ?? m.operator}
              </option>
            ))}
          </optgroup>
          <optgroup label="Personas">
            {(personas.data ?? []).map((p) => (
              <option key={String(p.id)} value={`persona:${p.id}`}>
                {p.name}
              </option>
            ))}
          </optgroup>
          <optgroup label="Pods">
            {(pods.data ?? []).map((p) => (
              <option key={String(p.id)} value={`pod:${p.id}`}>
                {p.name}
              </option>
            ))}
          </optgroup>
        </select>
      </label>
      {!!plan.data && (
        <div style={{ marginTop: 8 }} data-testid="issue-dispatch-plan">
          {plan.data.dispatchable ? (
            <button className="btn primary small" onClick={() => void dispatch()}>
              Dispatch ▷
            </button>
          ) : plan.data.in_flight_session_id ? (
            <div className="meta">A session is already running this issue.</div>
          ) : (
            <div className="meta" data-testid="issue-blocked">
              {blockedCopy(plan.data.blocked_reason)}
            </div>
          )}
          {plan.data.assignee_kind === "pod" && plan.data.candidates.length > 0 && (
            <div className="meta" style={{ marginTop: 4 }}>
              Pod routing considered{" "}
              {plan.data.candidates
                .map(
                  (c) =>
                    `${c.persona_slug ?? c.persona_id}${
                      c.dispatchable ? " (ready)" : ` (${blockedCopy(c.blocked_reason)})`
                    }`,
                )
                .join(", ")}
              .
            </div>
          )}
        </div>
      )}

      <div className="eyebrow" style={{ margin: "16px 0 8px" }}><span>Move to</span></div>
      <div className="row wrap">
        {BOARD_COLUMNS.filter((s) => s !== issue.status).map((s) => (
          <button key={s} className="btn small" onClick={() => void transition(s)}>
            {s}
          </button>
        ))}
        <button className="btn small danger" onClick={() => void transition("cancelled")}>
          cancel
        </button>
      </div>

      <div className="eyebrow" style={{ margin: "20px 0 8px" }}><span>Execution evidence</span></div>
      {evidence.data == null ? (
        <div className="meta">Execution evidence could not be loaded.</div>
      ) : (
        <div data-testid="issue-evidence">
          <SoftCard>
            <div className="meta">
              {evidence.data.totals.sessions} session
              {evidence.data.totals.sessions === 1 ? "" : "s"} ·{" "}
              {evidence.data.totals.events} durable event
              {evidence.data.totals.events === 1 ? "" : "s"} ·{" "}
              {evidence.data.totals.commands} command
              {evidence.data.totals.commands === 1 ? "" : "s"} ·{" "}
              {evidence.data.totals.decisions} decision
              {evidence.data.totals.decisions === 1 ? "" : "s"}
              {evidence.data.totals.open_decisions > 0
                ? ` (${evidence.data.totals.open_decisions} open)`
                : ""}
            </div>
            <div className="meta" style={{ marginTop: 4 }} data-testid="issue-usage">
              {evidence.data.usage.attributed_calls === 0
                ? `No gateway calls are attributed to this issue — ${evidence.data.usage.attribution}.`
                : `${evidence.data.usage.attributed_calls} attributed call(s) · ` +
                  `${evidence.data.usage.input_tokens} in / ${evidence.data.usage.output_tokens} out tokens` +
                  (evidence.data.usage.priced_calls > 0
                    ? ` · $${evidence.data.usage.cost_actual_usd.toFixed(4)}`
                    : "") +
                  (evidence.data.usage.unpriced_calls > 0
                    ? ` · ${evidence.data.usage.unpriced_calls} unpriced`
                    : "")}
            </div>
            {(evidence.data.totals.hidden_sessions ?? 0) > 0 && (
              <div className="meta" style={{ marginTop: 4 }}>
                {evidence.data.totals.hidden_sessions} session(s) are in a workspace you cannot
                read and are not counted above.
              </div>
            )}
          </SoftCard>

          <div className="eyebrow" style={{ margin: "16px 0 8px" }}><span>Sessions</span></div>
          {evidence.data.sessions.length === 0 ? (
            <div className="meta">No sessions have run this issue.</div>
          ) : (
            <div className="card-list">
              {evidence.data.sessions.map((s) => (
                <SoftCard key={String(s.id)} onClick={() => onOpenChat(s.id)} interactive>
                  <div className="row spread">
                    <span>{s.persona_name ?? s.tool ?? `session ${s.id}`}</span>
                    <StatusPill label={s.state ?? s.status ?? "—"} />
                  </div>
                  <div className="meta" style={{ marginTop: 4 }}>
                    {s.events ?? 0} events · {s.commands ?? 0} commands ·{" "}
                    {s.usage?.calls ?? 0} attributed calls
                  </div>
                </SoftCard>
              ))}
            </div>
          )}

          {evidence.data.decisions.length > 0 && (
            <>
              <div className="eyebrow" style={{ margin: "16px 0 8px" }}>
                <span>Decisions</span>
              </div>
              <div className="card-list">
                {evidence.data.decisions.map((d) => (
                  <SoftCard key={d.code}>
                    <div className="row spread">
                      <span>{d.title ?? d.code}</span>
                      <StatusPill label={d.status} />
                    </div>
                  </SoftCard>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      <div className="eyebrow" style={{ margin: "20px 0 8px" }}><span>Comments</span></div>
      <div className="card-list" data-testid="issue-comments">
        {(comments.data ?? []).length === 0 ? (
          <div className="meta">No comments yet.</div>
        ) : (
          (comments.data ?? []).map((c) => (
            <SoftCard key={String(c.id)}>
              <div className="row spread" style={{ marginBottom: 4 }}>
                <strong style={{ fontSize: 12 }}>
                  {c.author_kind === "persona"
                    ? `persona #${c.author_persona_id ?? "?"}`
                    : c.author_kind === "system"
                      ? "system"
                      : "operator"}
                </strong>
                {c.created_at && (
                  <span className="meta">{new Date(c.created_at).toLocaleString()}</span>
                )}
              </div>
              <div style={{ whiteSpace: "pre-wrap" }}>{c.body}</div>
            </SoftCard>
          ))
        )}
      </div>
      <div style={{ marginTop: 8 }}>
        <TextArea label="" value={draft} onChange={setDraft} placeholder="Add a comment…" />
        <button
          className="btn primary small"
          disabled={posting || !draft.trim()}
          onClick={() => void postComment()}
        >
          Comment
        </button>
      </div>
    </div>
  );
}

function CreateIssue({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { activeOrg } = useOrg();
  const { toast } = useToast();
  const projects = useAsync(
    () => (activeOrg ? api.listProjects(activeOrg.slug) : Promise.resolve([])),
    [activeOrg?.slug],
  );
  const [project, setProject] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [priority, setPriority] = useState("p2");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const proj = project || projects.data?.[0]?.code;
    if (!proj || !title.trim()) {
      toast("Pick a project and title");
      return;
    }
    setBusy(true);
    try {
      await api.createIssue(proj, { title: title.trim(), body, priority: priority as Issue["priority"] });
      toast("Issue created");
      onCreated();
    } catch (e) {
      toast(formatApiError("Create issue", e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer open onClose={onClose}>
      <div className="eyebrow"><span>New issue</span></div>
      <h2 style={{ margin: "8px 0 4px" }}>Create issue</h2>
      <p className="meta" style={{ marginBottom: 16 }}>
        Issues are created from these fields only. Brains does not turn a sentence into an
        issue, so nothing is inferred that you did not type.
      </p>
      <Select
        label="Project"
        value={project}
        onChange={setProject}
        options={[
          { value: "", label: projects.data?.[0]?.name ?? "— first project —" },
          ...(projects.data ?? []).map((p) => ({ value: p.code, label: p.name })),
        ]}
      />
      <TextField label="Title" value={title} onChange={setTitle} placeholder="Scaffold the SPA" />
      <TextArea label="Body" value={body} onChange={setBody} />
      <Select
        label="Priority"
        value={priority}
        onChange={setPriority}
        options={["p0", "p1", "p2", "p3"].map((p) => ({ value: p, label: p }))}
      />
      <div className="row" style={{ marginTop: 8 }}>
        <button className="btn primary" disabled={busy} onClick={() => void submit()}>
          Create
        </button>
        <button className="btn ghost" onClick={onClose}>
          Cancel
        </button>
      </div>
    </Drawer>
  );
}
