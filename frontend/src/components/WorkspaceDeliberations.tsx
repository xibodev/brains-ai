import { useLayoutEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { WorkspaceDeliberation, WorkspaceDeliberationSpec, WorkspaceWorkContribution } from "../api/types";
import { useOperator } from "../store/OperatorContext";
import { OperatorCard, OperatorState } from "./OperatorPrimitives";
import {
  useWorkCreationKey, useWorkMutation, useWorkRead, WorkAuthor, WorkCancel,
  workDeadline, WorkField, WorkMutationNotice, WorkRefresh, WorkStatus, WorkText,
  workTextError, workTime,
} from "./WorkspaceWorkShared";

export function WorkspaceDeliberations({ slug }: { slug: string }) {
  const list = useWorkRead((signal) => api.operatorWorkspaceDeliberations(slug, signal), [slug]);
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
  return <section id="workspace-deliberations" className="workspace-work-section" aria-label="Deliberations">
    <OperatorCard kicker="Existing peers" title="Deliberations" action={<button type="button" ref={createButton} className="operator-button primary" disabled={operator.loading || !operator.catalog || creating} aria-expanded={creating} onClick={() => setCreating(true)}>New proposal</button>}>
      <p className="workspace-work-intro">Ask 2–8 owned agent Sessions to investigate an objective independently, discuss findings, and record a synthesis. Agents acknowledge and contribute through their own harnesses.</p>
      {!operator.catalog && !operator.loading && <p className="workspace-work-error" role="alert">Operator access could not be loaded. <button type="button" className="operator-button" onClick={operator.refresh}>Retry operator access</button></p>}
      {creating && <DeliberationForm key={slug} slug={slug} created={(row) => {
        setSelected(row.code); setCreatedCode(row.code); closeCreation(); refresh();
      }} close={closeCreation} />}
      {createdCode && <p className="workspace-work-notice" role="status">Proposal recorded or retrieved: <code>{createdCode}</code>. Its current detail is open below.</p>}
      <WorkRefresh at={list.data?.refreshedAt} pending={list.loading} refresh={refresh} />
      <OperatorState loading={list.loading} error={list.error} kind={list.errorKind} empty={list.data?.value.length === 0} emptyTitle="No deliberations yet" emptyBody="Create a proposal with an explicit result owner and evidence expectations." />
      {list.data && <ul className="workspace-work-records">{list.data.value.map((row) => <li key={row.code}>
        <button type="button" className={`workspace-work-choice${selected === row.code ? " selected" : ""}`} aria-pressed={selected === row.code} onClick={() => setSelected(row.code)}>
          <span><strong>{row.title}</strong><code>{row.code}</code><small>Version {row.version} · {row.counts.acceptances}/{row.counts.required_acceptances} acknowledgements</small></span><WorkStatus status={row.status} />
        </button>
      </li>)}</ul>}
      {list.data?.value.length === 50 && <p className="workspace-work-meta">Showing up to 50 deliberations; refresh for the latest records.</p>}
      {selected && <DeliberationDetail key={selected} slug={slug} code={selected} epoch={detailEpoch} changed={list.refetch} />}
    </OperatorCard>
  </section>;
}

function DeliberationForm({ slug, created, close }: { slug: string; created: (row: WorkspaceDeliberation) => void; close: () => void }) {
  const peers = useWorkRead((signal) => api.operatorWorkParticipants(slug, signal), [slug]);
  const [title, setTitle] = useState("");
  const [objective, setObjective] = useState("");
  const [context, setContext] = useState("");
  const [evidence, setEvidence] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [models, setModels] = useState<Record<string, string>>({});
  const [owner, setOwner] = useState("");
  const [rounds, setRounds] = useState("1");
  const [deadline, setDeadline] = useState("");
  const key = useWorkCreationKey();
  const mutation = useWorkMutation();
  const options = peers.data?.value ?? [];
  const unavailable = selected.filter((id) => !options.some((peer) => peer.session_id === id));
  const toggle = (id: string) => {
    setSelected((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id]);
    if (owner === id) setOwner("");
  };
  return <form className="workspace-work-form" onChange={key.edited} onSubmit={(event) => {
    event.preventDefault();
    if (mutation.pending) return;
    const error = workTextError(title, "Title", true, 256) || workTextError(objective, "Objective", true)
      || workTextError(context, "Context", true) || workTextError(evidence, "Evidence expectations", true);
    if (error) { mutation.setError(error); return; }
    if (selected.length < 2 || selected.length > 8 || (!key.isRetry() && (peers.loading || peers.error || unavailable.length))) {
      mutation.setError("Choose 2–8 distinct Sessions from the current owned-participant list. Refresh if needed."); return;
    }
    if (!selected.includes(owner)) { mutation.setError("Choose a result owner from the selected participants."); return; }
    const discussionRounds = Number(rounds);
    if (!Number.isInteger(discussionRounds) || discussionRounds < 0 || discussionRounds > 3) {
      mutation.setError("Discussion rounds must be a whole number from 0 to 3."); return;
    }
    for (const id of selected) {
      const problem = workTextError(models[id] || "", "Declared model", false, 128);
      if (problem) { mutation.setError(problem); return; }
    }
    try {
      const specification: WorkspaceDeliberationSpec = {
        version: 1, objective, context, evidence_expectations: evidence,
        participants: [...selected].sort().map((id) => ({ session_id: id, model: models[id]?.trim() || null })),
        result_owner_session_id: owner, discussion_rounds: discussionRounds,
        // An unchanged creation retry may retrieve an already-recorded proposal
        // after its deadline. The server checks idempotency before new-work time bounds.
        ...(deadline ? { deadline: workDeadline(deadline, !key.isRetry()) } : {}),
      };
      const problem = workTextError(JSON.stringify(specification), "Complete specification");
      if (problem) { mutation.setError(problem); return; }
      void mutation.run("Create proposal", (signal) => api.operatorCreateDeliberation(slug, {
        title, specification, idempotency_key: key.get(),
      }, signal), created);
    } catch (reason) { mutation.setError(reason instanceof Error ? reason.message : "Check the deadline."); }
  }}>
    <h3>New proposal</h3><p className="workspace-work-meta">Recorded as you. Human agreement creates the proposal; every selected agent must acknowledge its exact specification.</p>
    <fieldset disabled={mutation.pending}>
      <WorkField label="Title" required><input autoFocus required maxLength={256} value={title} onChange={(event) => setTitle(event.target.value)} /></WorkField>
      <WorkField label="Objective" required><textarea required value={objective} onChange={(event) => setObjective(event.target.value)} /></WorkField>
      <WorkField label="Context" required><textarea required value={context} onChange={(event) => setContext(event.target.value)} /></WorkField>
      <WorkField label="Evidence expectations" required hint="What should each agent cite or demonstrate?"><textarea required value={evidence} onChange={(event) => setEvidence(event.target.value)} /></WorkField>
      <fieldset className="workspace-work-peers"><legend>Participants (required · choose 2–8)</legend>
        <p className="workspace-work-meta">Owned, live Sessions reported by the server. Selection invites participation; it does not act as that agent.</p>
        <WorkRefresh at={peers.data?.refreshedAt} pending={peers.loading || mutation.pending} refresh={peers.refetch} />
        <OperatorState loading={peers.loading} error={peers.error} kind={peers.errorKind} empty={peers.data?.value.length === 0} emptyTitle="No eligible agent Sessions" emptyBody="Open agent harnesses connected to this Workspace, then refresh participants." />
        {options.map((peer) => <div className="workspace-work-peer" key={peer.session_id}>
          <label className="workspace-work-check"><input type="checkbox" checked={selected.includes(peer.session_id)} disabled={!selected.includes(peer.session_id) && selected.length >= 8} onChange={() => toggle(peer.session_id)} /><span><strong>{peer.tool}</strong><code>{peer.session_id}</code><small>{peer.state} · Last activity {workTime(peer.last_activity_at)} · Started {workTime(peer.started_at)}</small></span></label>
          {selected.includes(peer.session_id) && <WorkField label={`Declared model for ${peer.tool} · ${peer.session_id}`} hint="Optional · declared, not verified. This does not route or change the agent's model."><input maxLength={128} value={models[peer.session_id] || ""} onChange={(event) => setModels({ ...models, [peer.session_id]: event.target.value })} /></WorkField>}
        </div>)}
        {peers.data && unavailable.length > 0 && <div role="alert" className="workspace-work-error"><p>Some selected Sessions are no longer available. Remove them and choose current participants.</p>{unavailable.map((id) => <button key={id} type="button" className="operator-button" onClick={() => { toggle(id); key.edited(); }}>Remove {id}</button>)}</div>}
        {options.length === 200 && <p className="workspace-work-meta">Showing up to 200 eligible Sessions.</p>}
        <p className="workspace-work-meta" role="status">{selected.length} of 8 selected · minimum 2</p>
      </fieldset>
      <WorkField label="Result owner" required hint="Select a participant to record the final synthesis through its own harness.">
        <select required value={owner} onChange={(event) => setOwner(event.target.value)}>
          <option value="">Choose result owner</option>
          {/* Keep the controlled value valid for native required validation after
              a lost create response and participant refresh. New/edited requests
              still fail the unavailable-participant checks above. */}
          {owner && selected.includes(owner) && unavailable.includes(owner) && <option value={owner}>{owner} · unavailable; previously selected; retry only</option>}
          {options.filter((peer) => selected.includes(peer.session_id)).map((peer) => <option key={peer.session_id} value={peer.session_id}>{peer.tool} · {peer.session_id}</option>)}
        </select>
      </WorkField>
      <div className="workspace-work-fields">
        <WorkField label="Discussion rounds" required><input type="number" min={0} max={3} step={1} required value={rounds} onChange={(event) => setRounds(event.target.value)} /></WorkField>
        <WorkField label="Deadline" hint="Leave blank for one hour from creation. Local time saved as UTC; maximum 30 days."><input type="datetime-local" value={deadline} onChange={(event) => setDeadline(event.target.value)} /></WorkField>
      </div>
      <div className="operator-action-row"><button className="operator-button primary" disabled={selected.length < 2 || selected.length > 8 || !selected.includes(owner) || (!key.isRetry() && (peers.loading || !!peers.error || !!unavailable.length))}>{mutation.pending ? "Recording…" : "Create proposal"}</button><button type="button" className="operator-button" onClick={close}>Close form</button></div>
    </fieldset>
    <WorkMutationNotice mutation={mutation} canReview={false} />
    {mutation.error && <p className="workspace-work-meta">If the response was lost, submit the unchanged form again to retrieve the same proposal. Editing starts a new request.</p>}
  </form>;
}

function DeliberationDetail({ slug, code, epoch, changed }: { slug: string; code: string; epoch: number; changed: () => void }) {
  const [version, setVersion] = useState<number | undefined>();
  const [requestedVersion, setRequestedVersion] = useState("");
  const [versionError, setVersionError] = useState<string | null>(null);
  // Read latest separately so historical selection never invents available_versions
  // or allocates thousands of options. The number input is bounded by this read.
  const latest = useWorkRead((signal) => api.operatorWorkspaceDeliberation(slug, code, undefined, signal), [slug, code, epoch]);
  const history = useWorkRead((signal) => version === undefined ? Promise.resolve(null) : api.operatorWorkspaceDeliberation(slug, code, version, signal), [slug, code, version, epoch]);
  const detail = version === undefined ? latest : history;
  const row = detail.data?.value;
  const mutation = useWorkMutation();
  const refresh = () => { latest.refetch(); if (version !== undefined) history.refetch(); };
  const refreshLatest = () => { setVersion(undefined); setRequestedVersion(""); latest.refetch(); changed(); };
  const blocked = mutation.pending || mutation.conflict || detail.loading || !!detail.error || version !== undefined;
  return <div className="workspace-work-detail" aria-label="Deliberation detail" aria-busy={detail.loading || mutation.pending}>
    <WorkRefresh at={detail.data?.refreshedAt} pending={detail.loading || mutation.pending} refresh={refresh} />
    <form className="workspace-work-version" onSubmit={(event) => {
      event.preventDefault();
      const candidate = Number(requestedVersion);
      if (!Number.isInteger(candidate) || candidate < 1 || !latest.data || candidate > latest.data.value.version) {
        setVersionError("Enter a version from 1 to the latest recorded version."); return;
      }
      setVersionError(null); setVersion(candidate);
    }}>
      <WorkField label={`Historical version${latest.data ? ` (1–${latest.data.value.version})` : ""}`}><input type="number" required min={1} max={latest.data?.value.version} step={1} value={requestedVersion} disabled={mutation.pending || mutation.conflict} onChange={(event) => setRequestedVersion(event.target.value)} /></WorkField>
      <button className="operator-button" disabled={mutation.pending || mutation.conflict || !latest.data}>View version</button>
      <button type="button" className="operator-button" disabled={mutation.pending} onClick={() => { setVersion(undefined); setRequestedVersion(""); setVersionError(null); latest.refetch(); }}>View latest</button>
    </form>
    {versionError && <p className="workspace-work-error" role="alert">{versionError}</p>}
    <OperatorState loading={detail.loading} error={detail.error} kind={detail.errorKind} />
    <WorkMutationNotice mutation={mutation} canReview={!!row && !detail.loading && !detail.error && version === undefined} />
    {row && <>
      <header className="workspace-work-detail-head"><div><h3>{row.title}</h3><code>{row.code}</code></div><WorkStatus status={row.status} /></header>
      <WorkAuthor kind={row.creator_kind} session={row.creator_session_id} />
      {version !== undefined && <p className="workspace-work-notice">Viewing version {row.version} in read-only mode. Choose “View latest” to act on the current proposal.</p>}
      <dl className="workspace-work-facts">
        <div><dt>Version / revision</dt><dd>{row.version} / {row.revision}</dd></div>
        <div><dt>Discussion round</dt><dd>{row.round} · {row.specification.discussion_rounds} planned</dd></div>
        <div><dt>Deadline</dt><dd>{workTime(row.deadline)}</dd></div>
        <div><dt>Created</dt><dd>{workTime(row.created_at)}</dd></div>
        <div><dt>Result owner</dt><dd><code>{row.result_owner_session_id}</code></dd></div>
        <div><dt>Outcome</dt><dd>{row.incomplete_flag ? "Incomplete" : "Synthesis recorded"}</dd></div>
      </dl>
      {row.expired_flag && <p className="workspace-work-notice">Deadline exceeded. This proposal remains incomplete; expiry does not settle work or stop agent processes.</p>}
      {row.final_ready && <p className="workspace-work-notice">Ready for the result owner to submit a final synthesis through its own harness.</p>}
      <WorkText label="Objective" value={row.specification.objective} /><WorkText label="Context" value={row.specification.context} /><WorkText label="Evidence expectations" value={row.specification.evidence_expectations} />
      {!!row.specification.links?.length && <WorkText label="References" value={row.specification.links.join("\n")} />}
      <details className="workspace-work-disclosure"><summary>Specification identity</summary><WorkText label="Specification hash" value={row.spec_hash} /></details>
      <h3>Peer progress</h3>
      <p className="workspace-work-meta">{row.counts.acceptances}/{row.counts.required_acceptances} acknowledgements · {row.counts.initial}/{row.counts.participants} initial reports · {row.counts.discussion} reports in the current discussion round</p>
      <ul className="workspace-work-participant-progress">{row.specification.participants.map((peer) => <li key={peer.session_id}>
        <strong>{peer.tool}</strong><code>{peer.session_id}</code><span>Model: {peer.model || "not declared"}{peer.model && " · declared, not verified"}</span>
        <span>{row.accepted_session_ids.includes(peer.session_id) ? "Acknowledged exact specification" : "Awaiting acknowledgement"}</span>
        <span>{row.remaining_initial_session_ids.includes(peer.session_id) ? "Initial report pending" : "Initial report received"}</span>
        {row.remaining_discussion_session_ids.includes(peer.session_id) && <span>Discussion report pending</span>}
      </li>)}</ul>
      {row.remaining_acceptance_session_ids.some((id) => !row.specification.participants.some((peer) => peer.session_id === id)) && <WorkText label="Other required acknowledgements pending" value={row.remaining_acceptance_session_ids.filter((id) => !row.specification.participants.some((peer) => peer.session_id === id)).join("\n")} />}
      <div className="workspace-work-actions">
        <p>{advanceDescription(row)}</p>
        <button type="button" className="operator-button primary" disabled={blocked || !row.permissions.can_advance} onClick={() => {
          const captured = row;
          void mutation.run("Advance proposal", (signal) => api.operatorAdvanceDeliberation(slug, code, captured.version, captured.revision, signal), (result) => {
            mutation.setFeedback(result.revision === captured.revision ? "No change: the protocol was already at this stage." : "Protocol advanced. Review the refreshed stage and peer progress."); refreshLatest();
          }, refreshLatest);
        }}>{mutation.pending ? "Recording…" : advanceLabel(row)}</button>
        {!row.permissions.can_advance && <p className="workspace-work-meta">Advance unavailable: {row.permissions.advance_blocked_reason?.replaceAll("_", " ") || "not permitted"}.</p>}
        <WorkCancel disabled={blocked || !row.permissions.can_cancel} pending={mutation.pending} reasonRequired label="Cancel proposal" confirm={(reason) => {
          const captured = row;
          void mutation.run("Cancel proposal", (signal) => api.operatorCancelDeliberation(slug, code, reason, captured.version, captured.revision, signal), (result) => {
            mutation.setFeedback(result.revision === captured.revision ? "No change: this cancellation was already recorded." : "Proposal cancellation recorded. Agent processes are not stopped by this action."); refreshLatest();
          }, refreshLatest);
        }} />
        {!row.permissions.can_cancel && <p className="workspace-work-meta">Cancellation unavailable: {row.permissions.cancel_blocked_reason?.replaceAll("_", " ") || "not permitted"}.</p>}
      </div>
      {row.cancellation_reason && <WorkText label="Cancellation reason" value={row.cancellation_reason} />}
      <h3>Reports and evidence</h3>
      {row.blinded || !row.initial_closed ? <p className="workspace-work-notice">Initial reports are withheld until explicit initial closure, including from you. {row.counts.initial} received; {row.counts.contributions} contributions recorded. Cancellation does not reveal withheld reports.</p> : <>
        <p className="workspace-work-meta">Initial collection is closed. {row.counts.visible_contributions} visible contributions. Evidence below is supplied by agents.</p>
        {!row.contributions.length && <p>No visible reports recorded.</p>}
        {row.contributions.filter((entry) => entry.kind !== "final").map((entry) => <Contribution key={entry.contribution_id} entry={entry} />)}
        <h3>Unresolved dissent</h3>
        {!row.unresolved_dissent.length && <p>No dissent recorded in the visible reports.</p>}
        {row.unresolved_dissent.map((entry) => <article className="workspace-work-attempt" key={entry.contribution_id}><p><code>{entry.author_session_id}</code> · round {entry.round} · unresolved</p><WorkText label="Original dissent" value={entry.dissent} /></article>)}
        <h3>Final synthesis</h3><p className="workspace-work-meta">Result owner <code>{row.result_owner_session_id}</code>. Synthesis does not resolve original dissent.</p>
        {row.final ? <><WorkText label="Final summary" value={row.final.summary} /><WorkText label="Final evidence" value={row.final.evidence} /></> : <p>No final synthesis recorded.</p>}
      </>}
    </>}
  </div>;
}

function advanceLabel(row: WorkspaceDeliberation) {
  if (row.status === "accepted") return "Begin initial collection";
  if (row.status === "collecting") return "Close initial collection";
  if (row.status === "discussing" && !row.final_ready) return `Close discussion round ${row.round}`;
  return "Advance protocol";
}

function advanceDescription(row: WorkspaceDeliberation) {
  if (row.status === "planned") return "All required agent Sessions must acknowledge this version before initial collection can begin.";
  if (row.status === "accepted") return "Begin independent initial reports after all required acknowledgements.";
  if (row.status === "collecting") return "Closing initial collection reveals the submitted reports and moves to discussion or final synthesis.";
  if (row.status === "discussing" && !row.final_ready) return "Close this round after all required discussion reports are recorded.";
  return "The server determines which protocol transitions are currently available.";
}

function Contribution({ entry }: { entry: WorkspaceWorkContribution }) {
  if (entry.kind === "final") return null;
  return <article className="workspace-work-attempt">
    <h4>{entry.kind === "initial" ? "Initial report" : `Discussion round ${entry.round}`}</h4>
    <p className="workspace-work-meta"><code>{entry.author_session_id}</code> · {workTime(entry.created_at)}<br /><code>{entry.contribution_id}</code></p>
    <WorkText label="Findings" value={entry.payload.findings} /><WorkText label="Evidence" value={entry.payload.evidence} /><WorkText label="Uncertainty" value={entry.payload.uncertainty} /><WorkText label="Dissent" value={entry.payload.dissent} />
    {!!entry.payload.clarifications?.length && <WorkText label="Clarifications" value={entry.payload.clarifications.join("\n")} />}
  </article>;
}
