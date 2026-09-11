import { useLayoutEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { WorkspaceAssignment, WorkspaceAssignmentSpec } from "../api/types";
import { useOperator } from "../store/OperatorContext";
import { OperatorCard, OperatorState } from "./OperatorPrimitives";
import {
  useWorkCreationKey, useWorkMutation, useWorkRead, WorkAuthor, WorkCancel,
  workDeadline, WorkField, WorkMutationNotice, WorkRefresh, WorkStatus, WorkText,
  workTextError, workTime,
} from "./WorkspaceWorkShared";

export function WorkspaceAssignments({ slug }: { slug: string }) {
  const list = useWorkRead((signal) => api.operatorWorkspaceAssignments(slug, signal), [slug]);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createdCode, setCreatedCode] = useState<string | null>(null);
  const [detailEpoch, setDetailEpoch] = useState(0);
  const createButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef(false);
  useLayoutEffect(() => {
    if (!creating && returnFocus.current) {
      returnFocus.current = false;
      createButton.current?.focus();
    }
  }, [creating]);
  const closeCreation = () => {
    returnFocus.current = true;
    setCreating(false);
  };
  const operator = useOperator();
  const refresh = () => { list.refetch(); setDetailEpoch((value) => value + 1); };
  return <section id="workspace-assignments" className="workspace-work-section" aria-label="Assignments">
    <OperatorCard kicker="Local work" title="Assignments" action={<button type="button" ref={createButton} className="operator-button primary" disabled={operator.loading || !operator.catalog || creating} aria-expanded={creating} onClick={() => setCreating(true)}>New assignment</button>}>
      <p className="workspace-work-intro">Record an objective and inspect agent-reported attempts and evidence. An existing agent accepts the assignment through its own harness.</p>
      {!operator.catalog && !operator.loading && <p className="workspace-work-error" role="alert">Operator access could not be loaded. <button type="button" className="operator-button" onClick={operator.refresh}>Retry operator access</button></p>}
      {creating && <AssignmentForm key={slug} slug={slug} created={(row) => {
        setSelected(row.code); setCreatedCode(row.code); closeCreation(); refresh();
      }} close={closeCreation} />}
      {createdCode && <p className="workspace-work-notice" role="status">Assignment recorded or retrieved: <code>{createdCode}</code>. Its current detail is open below.</p>}
      <WorkRefresh at={list.data?.refreshedAt} pending={list.loading} refresh={refresh} />
      <OperatorState loading={list.loading} error={list.error} kind={list.errorKind} empty={list.data?.value.length === 0} emptyTitle="No assignments yet" emptyBody="Create an assignment to record the work you want an agent to accept." />
      {list.data && <ul className="workspace-work-records">{list.data.value.map((row) => <li key={row.code}>
        <button type="button" className={`workspace-work-choice${selected === row.code ? " selected" : ""}`} aria-pressed={selected === row.code} onClick={() => setSelected(row.code)}>
          <span><strong>{row.title}</strong><code>{row.code}</code></span><WorkStatus status={row.observed_status} assignment />
        </button>
      </li>)}</ul>}
      {list.data?.value.length === 50 && <p className="workspace-work-meta">Showing up to 50 assignments; refresh for the latest records.</p>}
      {selected && <AssignmentDetail key={selected} slug={slug} code={selected} epoch={detailEpoch} changed={list.refetch} />}
    </OperatorCard>
  </section>;
}

function AssignmentForm({ slug, created, close }: { slug: string; created: (row: WorkspaceAssignment) => void; close: () => void }) {
  const [title, setTitle] = useState("");
  const [objective, setObjective] = useState("");
  const [context, setContext] = useState("");
  const [checkout, setCheckout] = useState("");
  const [runtime, setRuntime] = useState("3600");
  const [deadline, setDeadline] = useState("");
  const key = useWorkCreationKey();
  const mutation = useWorkMutation();
  return <form className="workspace-work-form" onChange={key.edited} onSubmit={(event) => {
    event.preventDefault();
    if (mutation.pending) return;
    const error = workTextError(title, "Title", true, 256) || workTextError(objective, "Objective", true)
      || workTextError(context, "Context") || workTextError(checkout, "Checkout reference", false, 2048);
    if (error) { mutation.setError(error); return; }
    const seconds = Number(runtime);
    if (!Number.isInteger(seconds) || seconds < 1 || seconds > 604800) {
      mutation.setError("Runtime must be a whole number from 1 to 604800 seconds."); return;
    }
    try {
      const specification: WorkspaceAssignmentSpec = {
        version: 1, objective, context, max_runtime_seconds: seconds,
        ...(checkout ? { checkout_ref: checkout } : {}),
        ...(deadline ? { deadline: workDeadline(deadline) } : {}),
      };
      const problem = workTextError(JSON.stringify(specification), "Complete specification");
      if (problem) { mutation.setError(problem); return; }
      void mutation.run("Create assignment", (signal) => api.operatorCreateAssignment(slug, {
        title, specification, idempotency_key: key.get(),
      }, signal), created);
    } catch (reason) { mutation.setError(reason instanceof Error ? reason.message : "Check the deadline."); }
  }}>
    <h3>New assignment</h3><p className="workspace-work-meta">Recorded as you. Agent acceptance and evidence are recorded separately.</p>
    <fieldset disabled={mutation.pending}>
      <WorkField label="Title" required><input autoFocus required maxLength={256} value={title} onChange={(event) => setTitle(event.target.value)} /></WorkField>
      <WorkField label="Objective" required><textarea required value={objective} onChange={(event) => setObjective(event.target.value)} /></WorkField>
      <WorkField label="Context" hint="Optional. This text is saved with the immutable specification."><textarea value={context} onChange={(event) => setContext(event.target.value)} /></WorkField>
      <WorkField label="Checkout reference" hint="Optional reference only; this does not claim or manage a checkout."><input value={checkout} maxLength={2048} onChange={(event) => setCheckout(event.target.value)} /></WorkField>
      <div className="workspace-work-fields">
        <WorkField label="Maximum runtime (seconds)" required hint="Cooperative limit, not an operating-system budget."><input type="number" required min={1} max={604800} step={1} value={runtime} onChange={(event) => setRuntime(event.target.value)} /></WorkField>
        <WorkField label="Deadline" hint="Optional. Your local time is saved as UTC."><input type="datetime-local" value={deadline} onChange={(event) => setDeadline(event.target.value)} /></WorkField>
      </div>
      <div className="operator-action-row"><button className="operator-button primary">{mutation.pending ? "Recording…" : "Create assignment"}</button><button type="button" className="operator-button" onClick={close}>Close form</button></div>
    </fieldset>
    <WorkMutationNotice mutation={mutation} canReview={false} />
    {mutation.error && <p className="workspace-work-meta">If the response was lost, submit the unchanged form again to retrieve the same assignment. Editing starts a new request.</p>}
  </form>;
}

function AssignmentDetail({ slug, code, epoch, changed }: { slug: string; code: string; epoch: number; changed: () => void }) {
  const detail = useWorkRead((signal) => api.operatorWorkspaceAssignment(slug, code, signal), [slug, code, epoch]);
  const row = detail.data?.value;
  const mutation = useWorkMutation();
  return <div className="workspace-work-detail" aria-label="Assignment detail" aria-busy={detail.loading || mutation.pending}>
    <WorkRefresh at={detail.data?.refreshedAt} pending={detail.loading || mutation.pending} refresh={detail.refetch} />
    <OperatorState loading={detail.loading} error={detail.error} kind={detail.errorKind} />
    <WorkMutationNotice mutation={mutation} canReview={!!row && !detail.loading && !detail.error} />
    {row && <>
      <header className="workspace-work-detail-head"><div><h3>{row.title}</h3><code>{row.code}</code></div><WorkStatus status={row.observed_status} assignment /></header>
      <WorkAuthor kind={row.creator_kind} session={row.creator_session_id} />
      <dl className="workspace-work-facts">
        <div><dt>Revision / generation</dt><dd>{row.revision} / {row.generation}</dd></div>
        <div><dt>Stored assignment state</dt><dd>{row.status.replaceAll("_", " ")}</dd></div>
        <div><dt>Created</dt><dd>{workTime(row.created_at)}</dd></div>
        <div><dt>Deadline</dt><dd>{workTime(row.specification.deadline)}</dd></div>
        <div><dt>Maximum runtime</dt><dd>{row.specification.max_runtime_seconds ?? 3600} seconds</dd></div>
      </dl>
      {row.observed_status === "uncertain" && <p className="workspace-work-notice">The outcome is uncertain. An exceeded deadline or unavailable source Session does not settle the work or start a retry.</p>}
      {row.deadline_exceeded && <p className="workspace-work-notice">Attempt deadline exceeded.</p>}
      {row.cancel_requested_at && <p className="workspace-work-notice">Cancellation requested {workTime(row.cancel_requested_at)}. This records a request, not proof of a stopped process.</p>}
      <WorkText label="Objective" value={row.specification.objective} />
      <WorkText label="Context" value={row.specification.context} />
      {row.specification.checkout_ref && <WorkText label="Checkout reference · no ownership" value={row.specification.checkout_ref} />}
      {row.specification.tool && <WorkText label="Requested tool" value={row.specification.tool} />}
      {!!row.specification.links?.length && <WorkText label="References" value={row.specification.links.join("\n")} />}
      <details className="workspace-work-disclosure"><summary>Specification identity</summary><WorkText label="Specification hash" value={row.specification_hash} /><p>Specification version {row.spec_version}</p></details>
      <div className="workspace-work-actions">
        <WorkCancel disabled={!row.permissions.can_cancel || mutation.conflict || detail.loading} pending={mutation.pending} confirm={() => {
          const captured = row;
          void mutation.run("Cancel assignment", (signal) => api.operatorCancelAssignment(slug, captured.code, captured.revision, signal), (result) => {
            mutation.setFeedback(result.revision === captured.revision ? "No change: cancellation was already recorded." : "Cancellation recorded. Review the refreshed assignment below.");
            detail.refetch(); changed();
          }, () => { detail.refetch(); changed(); });
        }} />
        {!row.permissions.can_cancel && <p className="workspace-work-meta">Cancellation unavailable: {row.permissions.reason?.replaceAll("_", " ") || "not permitted"}.</p>}
      </div>
      <h3>Attempt history <span className="workspace-work-meta">({row.attempts.length})</span></h3>
      {!row.attempts.length && <p>No agent has accepted this assignment yet.</p>}
      {row.attempts.map((attempt) => <article className="workspace-work-attempt" key={attempt.attempt_id}>
        <header className="workspace-work-detail-head"><h4>Attempt {attempt.generation} · {attempt.tool}</h4><WorkStatus status={attempt.observed_status} assignment /></header>
        <p className="workspace-work-meta"><code>{attempt.attempt_id}</code><br />Agent Session <code>{attempt.source_session_id}</code></p>
        <dl className="workspace-work-facts">
          <div><dt>Stored state</dt><dd>{attempt.status.replaceAll("_", " ")}</dd></div>
          <div><dt>Accepted</dt><dd>{workTime(attempt.accepted_at)}</dd></div>
          <div><dt>Deadline</dt><dd>{workTime(attempt.deadline_at)}</dd></div>
          <div><dt>Reported</dt><dd>{workTime(attempt.reported_at)}</dd></div>
          <div><dt>Settled</dt><dd>{workTime(attempt.settled_at)}</dd></div>
          <div><dt>Maximum runtime</dt><dd>{attempt.max_runtime_seconds} seconds</dd></div>
        </dl>
        {attempt.deadline_exceeded && <p className="workspace-work-notice">Deadline exceeded; outcome remains unresolved.</p>}
        {attempt.source_session_unavailable && <p className="workspace-work-notice">Source Session unavailable; outcome remains unresolved.</p>}
        {attempt.cancel_requested_at && <p>Cancellation requested {workTime(attempt.cancel_requested_at)}.</p>}
        <WorkText label="Agent-reported evidence" value={attempt.evidence} /><WorkText label="Agent-reported result" value={attempt.result} />
        {attempt.usage && <details className="workspace-work-disclosure"><summary>Reported usage</summary><WorkText label="Usage supplied by agent" value={JSON.stringify(attempt.usage, null, 2)} /></details>}
      </article>)}
    </>}
  </div>;
}
