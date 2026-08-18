import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, formatApiError } from "../api/client";
import type { Persona, Pod, PodDispatchPlan } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { useAsync } from "../store/useAsync";
import { useTopic } from "../realtime/useRealtime";
import { ScreenHead } from "./ScreenHead";
import { SoftCard } from "../components/SoftCard";
import { Loading, EmptyState } from "../components/EmptyState";
import { Drawer } from "../components/Drawer";
import { StatusPill } from "../components/StatusPill";
import { TextField } from "../components/Field";
import { useToast } from "../components/Toast";
import { blockedCopy } from "../components/blockedReason";

// Pods — teams of Personas with one leader (F5 / BL-P1-03). The roster is
// Personas, not operator labels: routing work to a human's login says nothing
// about which model, tool and machine will run it.
export function Pods() {
  const { activeOrg } = useOrg();
  const { toast } = useToast();
  const navigate = useNavigate();
  const { slug: routeSlug } = useParams();
  const [selected, setSelected] = useState<Pod | null>(null);
  const [creating, setCreating] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  const state = useAsync<Pod[]>(
    () =>
      activeOrg
        ? api.listPods(activeOrg.slug, showArchived ? { status: "all" } : undefined)
        : Promise.resolve([]),
    [activeOrg?.slug, showArchived],
  );
  useTopic(activeOrg ? `org/${activeOrg.slug}/pods` : null, () => state.refetch());

  // Deep link: /app/pods/:slug selects that Pod, and an unknown slug says so
  // rather than silently rendering the list (AC-F0-05).
  const [deepLinkMissing, setDeepLinkMissing] = useState(false);
  useEffect(() => {
    if (!routeSlug || state.data === undefined) {
      setDeepLinkMissing(false);
      return;
    }
    const match = (state.data ?? []).find((pod) => pod.slug === routeSlug);
    setDeepLinkMissing(!match);
    if (match) setSelected(match);
  }, [routeSlug, state.data]);

  const close = () => {
    setSelected(null);
    if (routeSlug) navigate("/pods");
  };

  const pods = state.data ?? [];

  return (
    <div>
      <ScreenHead
        eyebrow="Pods"
        title="Teams of personas"
        actions={
          <button className="btn primary" onClick={() => setCreating(true)}>
            + New pod
          </button>
        }
      />

      <div className="row wrap" style={{ marginBottom: 16 }}>
        <button
          className={`tab ${showArchived ? "active" : ""}`}
          onClick={() => setShowArchived((value) => !value)}
        >
          {showArchived ? "Hide archived" : "Show archived"}
        </button>
      </div>

      {deepLinkMissing && (
        <SoftCard>
          <div className="meta" data-testid="pod-not-found">
            No pod named <strong>{routeSlug}</strong> in this org.
          </div>
        </SoftCard>
      )}

      {state.loading && state.data === undefined ? (
        <Loading />
      ) : state.error ? (
        <EmptyState
          title="Pods could not be loaded"
          body={formatApiError("Load pods", state.error)}
          action={
            <button className="btn" onClick={() => state.refetch()}>
              Retry
            </button>
          }
        />
      ) : pods.length === 0 ? (
        <EmptyState
          title="No pods yet"
          body="Group personas into a Pod with a leader to route work as a team."
          action={
            <button className="btn primary" onClick={() => setCreating(true)}>
              + New pod
            </button>
          }
        />
      ) : (
        <div className="grid">
          {pods.map((pod) => (
            <div key={String(pod.id)} data-testid="pod-card">
              <SoftCard interactive onClick={() => navigate(`/pods/${pod.slug}`)}>
                <div className="row spread">
                  <strong>{pod.name}</strong>
                  <StatusPill label={pod.status ?? "active"} />
                </div>
                <div className="meta" style={{ marginTop: 6 }}>
                  {pod.members?.length ?? 0} persona members
                  {pod.leader_persona ? ` · leader: ${pod.leader_persona}` : " · no leader"}
                </div>
              </SoftCard>
            </div>
          ))}
        </div>
      )}

      {creating && activeOrg && (
        <CreatePodDrawer
          orgSlug={activeOrg.slug}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            state.refetch();
            toast("Pod created");
          }}
        />
      )}

      <PodDetailDrawer
        pod={selected}
        orgSlug={activeOrg?.slug ?? ""}
        onClose={close}
        onChanged={() => state.refetch()}
      />
    </div>
  );
}

function CreatePodDrawer({
  orgSlug,
  onClose,
  onCreated,
}: {
  orgSlug: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { toast } = useToast();
  const [name, setName] = useState("");
  const [slugOverride, setSlugOverride] = useState("");
  const [leader, setLeader] = useState("");
  const [busy, setBusy] = useState(false);

  const personas = useAsync<Persona[]>(
    () => api.listPersonas(orgSlug).catch(() => []),
    [orgSlug],
  );

  const derivedSlug =
    slugOverride || name.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "");

  const submit = async () => {
    if (!name.trim() || !leader) {
      toast("Name and leader persona are required");
      return;
    }
    setBusy(true);
    try {
      await api.createPod(orgSlug, {
        slug: derivedSlug || name.toLowerCase(),
        name: name.trim(),
        leader_persona_id: leader,
      });
      onCreated();
    } catch (e) {
      toast(formatApiError("Create pod", e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer open onClose={onClose}>
      <div className="eyebrow"><span>New pod</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>Create pod</h2>
      <TextField label="Name" value={name} onChange={setName} placeholder="Core team" />
      <TextField
        label="Slug (auto-derived)"
        value={slugOverride}
        onChange={setSlugOverride}
        placeholder={derivedSlug || "core-team"}
      />
      <label className="field">
        <span>Leader persona</span>
        <select value={leader} onChange={(e) => setLeader(e.target.value)}>
          <option value="">— select a persona —</option>
          {(personas.data ?? []).map((p) => (
            <option key={String(p.id)} value={String(p.id)}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      {(personas.data ?? []).length === 0 && (
        <div className="meta" style={{ marginBottom: 8 }}>
          This org has no personas yet — create one first, then it can lead a Pod.
        </div>
      )}
      <div className="row" style={{ marginTop: 16 }}>
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

function PodDetailDrawer({
  pod,
  orgSlug,
  onClose,
  onChanged,
}: {
  pod: Pod | null;
  orgSlug: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { toast } = useToast();
  const [newMember, setNewMember] = useState("");
  const [newLeader, setNewLeader] = useState("");
  const [busy, setBusy] = useState(false);

  const personas = useAsync<Persona[]>(
    () => (orgSlug ? api.listPersonas(orgSlug).catch(() => []) : Promise.resolve([])),
    [orgSlug],
  );

  const podDetail = useAsync<Pod | null>(
    () => (pod ? api.getPod(pod.id).catch(() => pod) : Promise.resolve(null)),
    [pod?.id],
  );

  const plan = useAsync<PodDispatchPlan | null>(
    () => (pod ? api.podDispatchPlan(pod.id).catch(() => null) : Promise.resolve(null)),
    [pod?.id],
  );

  // All hooks above — the early return follows them.
  if (!pod) return null;

  const live = podDetail.data ?? pod;
  const liveMembers = live.members ?? [];
  const legacy = live.legacy_operator_members ?? [];
  const archived = (live.status ?? "active") !== "active";

  const run = async (label: string, action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      toast(label);
      podDetail.refetch();
      plan.refetch();
      onChanged();
    } catch (e) {
      toast(formatApiError(label, e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer open={!!pod} onClose={onClose}>
      <div className="eyebrow"><span>Pod</span></div>
      <h2 style={{ margin: "8px 0 4px" }}>{live.name}</h2>
      {live.description && <p className="meta">{live.description}</p>}
      <div className="row wrap" style={{ marginBottom: 8 }}>
        <StatusPill label={live.status ?? "active"} />
        {live.leader_persona ? (
          <span className="meta">
            Leader persona: <strong>{live.leader_persona}</strong>
          </span>
        ) : (
          <span className="meta">No leader persona yet</span>
        )}
      </div>

      <div className="eyebrow" style={{ margin: "16px 0 8px" }}><span>Routing</span></div>
      <SoftCard>
        {plan.data == null ? (
          <div className="meta">Routing could not be resolved.</div>
        ) : plan.data.blocked_reason ? (
          <div className="meta" data-testid="pod-blocked">
            {blockedCopy(plan.data.blocked_reason)}
          </div>
        ) : (
          <div className="meta" data-testid="pod-routes-to">
            Work routes to <strong>{plan.data.persona_slug}</strong> on runtime #
            {String(plan.data.runtime_id)}.
          </div>
        )}
      </SoftCard>

      <div className="eyebrow" style={{ margin: "16px 0 8px" }}><span>Roster</span></div>
      {liveMembers.length === 0 ? (
        <div className="meta">No persona members yet.</div>
      ) : (
        <div className="card-list" data-testid="pod-roster">
          {liveMembers.map((m) => (
            <SoftCard key={String(m.persona_id)}>
              <div className="row spread">
                <span>{m.persona_name ?? m.name ?? `persona #${m.persona_id}`}</span>
                <div className="row">
                  {m.role && <StatusPill label={m.role} tone="accent" />}
                  {!m.is_leader && !archived && (
                    <button
                      className="btn ghost small"
                      disabled={busy}
                      onClick={() =>
                        void run("Member removed", () =>
                          api.removePodMember(live.id, m.persona_id),
                        )
                      }
                    >
                      Remove
                    </button>
                  )}
                </div>
              </div>
              <div className="meta" style={{ marginTop: 4 }}>
                {m.dispatchable
                  ? `ready on ${m.runtime_slug ?? "its runtime"}`
                  : blockedCopy(m.blocked_reason)}
              </div>
            </SoftCard>
          ))}
        </div>
      )}

      {legacy.length > 0 && (
        <>
          <div className="eyebrow" style={{ margin: "20px 0 8px" }}>
            <span>Legacy operator members</span>
          </div>
          <div className="card-list" data-testid="pod-legacy-members">
            {legacy.map((m) => (
              <SoftCard key={m.operator}>
                <div className="row spread">
                  <span>{m.name ?? m.operator}</span>
                  <StatusPill label="not routable" />
                </div>
                <div className="meta" style={{ marginTop: 4 }}>{m.reason}</div>
              </SoftCard>
            ))}
          </div>
        </>
      )}

      {!archived && (
        <>
          <div className="eyebrow" style={{ margin: "20px 0 8px" }}><span>Add member</span></div>
          <label className="field">
            <span>Persona</span>
            <select value={newMember} onChange={(e) => setNewMember(e.target.value)}>
              <option value="">— select —</option>
              {(personas.data ?? []).map((p) => (
                <option key={String(p.id)} value={String(p.id)}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <button
            className="btn primary small"
            disabled={busy || !newMember}
            onClick={() => {
              const persona = newMember;
              setNewMember("");
              void run("Member added", () => api.addPodMember(live.id, { persona_id: persona }));
            }}
            style={{ marginBottom: 20 }}
          >
            Add member
          </button>

          <div className="eyebrow" style={{ margin: "0 0 8px" }}><span>Set leader</span></div>
          <label className="field">
            <span>Leader persona</span>
            <select value={newLeader} onChange={(e) => setNewLeader(e.target.value)}>
              <option value="">— select —</option>
              {(personas.data ?? []).map((p) => (
                <option key={String(p.id)} value={String(p.id)}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <div className="row" style={{ marginTop: 8 }}>
            <button
              className="btn small"
              disabled={busy || !newLeader}
              onClick={() => {
                const persona = newLeader;
                setNewLeader("");
                void run("Leader updated", () =>
                  api.setPodLeader(live.id, { leader_persona_id: persona }),
                );
              }}
            >
              Set leader
            </button>
            <button
              className="btn small danger"
              disabled={busy}
              onClick={() => void run("Pod archived", () => api.archivePod(live.id))}
            >
              Archive pod
            </button>
          </div>
        </>
      )}
    </Drawer>
  );
}
