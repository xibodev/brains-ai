import { useState } from "react";
import { api } from "../api/client";
import { formatApiError } from "../api/client";
import type { Persona, Runtime, Session, SkillAttachment } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { useAsync } from "../store/useAsync";
import { useTopic } from "../realtime/useRealtime";
import { ScreenHead } from "./ScreenHead";
import { PersonaCard } from "../components/PersonaCard";
import { SoftCard } from "../components/SoftCard";
import { StatusPill } from "../components/StatusPill";
import { AsyncBoundary } from "../components/EmptyState";
import { Drawer } from "../components/Drawer";
import { TextField, TextArea, Select } from "../components/Field";
import { SkillAttachmentPanel } from "../components/SkillAttachmentPanel";
import { useToast } from "../components/Toast";

// Runtime -> Model -> Tool cascade shared by create and edit forms.
// Preserves a stored model/tool even when the bound runtime is offline.
function CascadeFields({
  runtimes,
  runtimeId,
  setRuntimeId,
  model,
  setModel,
  storedTool,
}: {
  runtimes: Runtime[];
  runtimeId: string;
  setRuntimeId: (id: string) => void;
  model: string;
  setModel: (m: string) => void;
  storedTool?: string | null;
}) {
  const onlineRuntimes = runtimes.filter((r) => r.status === "online");
  const selectedRuntime = runtimes.find((r) => String(r.id) === runtimeId);
  const runtimeModels: string[] = selectedRuntime?.capabilities?.models ?? [];
  // Preserve stored model as a selectable option even when its runtime is offline.
  const modelOptions = [...new Set([...(model ? [model] : []), ...runtimeModels])];
  const displayTool = selectedRuntime?.tool ?? storedTool ?? "";

  return (
    <>
      <Select
        label="Runtime"
        value={runtimeId}
        onChange={(v) => {
          setRuntimeId(v);
          setModel("");
        }}
        options={[
          { value: "", label: "-- select runtime --" },
          ...onlineRuntimes.map((r) => ({
            value: String(r.id),
            label: `${r.machine_label ?? r.machine_id ?? String(r.id)} · ${r.tool ?? "?"}`,
          })),
        ]}
      />
      <Select
        label="Model"
        value={model}
        onChange={setModel}
        disabled={modelOptions.length === 0}
        options={[
          {
            value: "",
            label: modelOptions.length ? "-- select model --" : "-- choose runtime first --",
          },
          ...modelOptions.map((m) => ({ value: m, label: m })),
        ]}
      />
      <div className="field">
        <span>Tool</span>
        <input type="text" value={displayTool} readOnly tabIndex={-1} />
      </div>
    </>
  );
}

// Personas — gallery of named operators -> detail/edit + spawn (WS4 §3.3).
export function Personas() {
  const { activeOrg } = useOrg();
  const { toast } = useToast();
  const [selected, setSelected] = useState<Persona | null>(null);
  const [creating, setCreating] = useState(false);
  const [liveIds, setLiveIds] = useState<Set<string>>(new Set());

  const state = useAsync<Persona[]>(
    () => (activeOrg ? api.listPersonas(activeOrg.slug) : Promise.resolve([])),
    [activeOrg?.slug],
  );

  useTopic(activeOrg ? `org/${activeOrg.slug}/personas` : null, () => state.refetch());
  // derive live dots from session lifecycle
  useTopic("org/default/sessions", () => {
    api
      .listSessions({ status: "running" })
      .then((ss: Session[]) =>
        setLiveIds(new Set(ss.map((s) => String(s.persona_id)).filter(Boolean))),
      )
      .catch(() => undefined);
  });

  const spawn = async (p: Persona) => {
    try {
      const res = (await api.spawnPersona(p.id, {})) as { status?: string };
      toast(res?.status === "pending_approval" ? "Spawn pending approval" : "Spawning...");
    } catch (e) {
      toast(formatApiError("Spawn", e));
    }
  };

  return (
    <div>
      <ScreenHead
        eyebrow="Personas"
        title="Your named operators"
        actions={
          <button className="btn primary" onClick={() => setCreating(true)}>
            + New persona
          </button>
        }
      />
      <AsyncBoundary
        state={state}
        emptyTitle="No personas yet"
        emptyBody="Create your first named AI operator — a brain (model) on a pair of hands (runtime)."
        emptyAction={
          <button className="btn primary" onClick={() => setCreating(true)}>
            + New persona
          </button>
        }
      >
        {(personas) => (
          <div className="card-list" style={{ maxWidth: 640 }}>
            {personas.map((p, i) => (
              <PersonaCard
                key={String(p.id)}
                persona={p}
                live={liveIds.has(String(p.id))}
                cascade={i > 0}
                onClick={() => setSelected(p)}
                action={
                  <button
                    className="btn primary small"
                    onClick={(e) => {
                      e.stopPropagation();
                      void spawn(p);
                    }}
                  >
                    Spawn
                  </button>
                }
              />
            ))}
          </div>
        )}
      </AsyncBoundary>

      <Drawer open={!!selected} onClose={() => setSelected(null)}>
        {selected && (
          <PersonaDetail
            persona={selected}
            onSaved={() => {
              state.refetch();
              setSelected(null);
            }}
            onDeleted={() => {
              state.refetch();
              setSelected(null);
            }}
          />
        )}
      </Drawer>

      {creating && (
        <CreatePersona
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

function PersonaDetail({
  persona,
  onSaved,
  onDeleted,
}: {
  persona: Persona;
  onSaved: () => void;
  onDeleted: () => void;
}) {
  const { toast } = useToast();
  const { activeOrg } = useOrg();
  const runtimes = useAsync<Runtime[]>(() => api.listRuntimes().catch(() => []), []);
  const sessions = useAsync(() => api.personaSessions(persona.id).catch(() => []), [persona.id]);
  const personaSkills = useAsync<SkillAttachment[]>(
    () => api.listPersonaSkills(persona.id).catch(() => []),
    [persona.id],
  );
  const [name, setName] = useState(persona.name);
  const [runtimeId, setRuntimeId] = useState(String(persona.default_runtime_id ?? ""));
  const [model, setModel] = useState(persona.model ?? "");
  const [color, setColor] = useState(persona.color ?? "");
  const [prompt, setPrompt] = useState(persona.system_prompt ?? "");
  const [busy, setBusy] = useState(false);

  const selectedRuntime = (runtimes.data ?? []).find((r) => String(r.id) === runtimeId);
  const derivedTool = selectedRuntime?.tool ?? persona.tool ?? "";

  const save = async () => {
    setBusy(true);
    try {
      await api.patchPersona(persona.id, {
        name,
        model,
        tool: derivedTool,
        default_runtime_id: runtimeId || null,
        system_prompt: prompt,
        color: color || null,
      });
      toast("Persona saved");
      onSaved();
    } catch (e) {
      toast(formatApiError("Save", e));
    } finally {
      setBusy(false);
    }
  };

  const archive = async () => {
    setBusy(true);
    try {
      await api.archivePersona(persona.id);
      toast("Persona archived");
      onDeleted();
    } catch (e) {
      toast(formatApiError("Archive", e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="eyebrow"><span>Persona</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>{persona.name}</h2>
      <TextField label="Name" value={name} onChange={setName} />
      <CascadeFields
        runtimes={runtimes.data ?? []}
        runtimeId={runtimeId}
        setRuntimeId={setRuntimeId}
        model={model}
        setModel={setModel}
        storedTool={persona.tool}
      />
      <TextField label="Color" value={color} onChange={setColor} placeholder="#8b5cf6" />
      <TextArea label="Instructions" value={prompt} onChange={setPrompt} />
      <div className="row" style={{ marginBottom: 12, gap: 8 }}>
        <button className="btn primary" disabled={busy} onClick={() => void save()}>
          Save
        </button>
        <button
          className="btn danger"
          disabled={busy}
          onClick={() => void archive()}
          style={{ marginLeft: "auto" }}
        >
          Delete
        </button>
      </div>
      <div className="eyebrow" style={{ marginBottom: 8 }}><span>Recent sessions</span></div>
      {(sessions.data ?? []).length === 0 ? (
        <div className="meta">No sessions yet.</div>
      ) : (
        <div className="card-list">
          {(sessions.data ?? []).map((s) => (
            <SoftCard key={String(s.id)}>
              <div className="row spread">
                <span>{s.tool ?? `session ${s.id}`}</span>
                <StatusPill label={s.status ?? "—"} />
              </div>
            </SoftCard>
          ))}
        </div>
      )}
      <div style={{ marginTop: 20 }}>
        <SkillAttachmentPanel
          orgSlug={activeOrg?.slug}
          attached={personaSkills}
          onAttach={(skillId) => api.attachPersonaSkill(persona.id, skillId)}
          onDetach={(skillId) => api.detachPersonaSkill(persona.id, skillId)}
        />
      </div>
    </div>
  );
}

function CreatePersona({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const { activeOrg } = useOrg();
  const { toast } = useToast();
  const runtimes = useAsync<Runtime[]>(() => api.listRuntimes().catch(() => []), []);
  const [name, setName] = useState("");
  const [runtimeId, setRuntimeId] = useState("");
  const [model, setModel] = useState("");
  const [color, setColor] = useState("");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);

  const selectedRuntime = (runtimes.data ?? []).find((r) => String(r.id) === runtimeId);
  const derivedTool = selectedRuntime?.tool ?? "";

  const submit = async () => {
    if (!activeOrg || !name.trim()) {
      toast("Name required");
      return;
    }
    setBusy(true);
    try {
      await api.createPersona(activeOrg.slug, {
        slug: name.trim().toLowerCase().replace(/\s+/g, "-"),
        name: name.trim(),
        model,
        tool: derivedTool,
        default_runtime_id: runtimeId || null,
        system_prompt: prompt || null,
        color: color || null,
      });
      toast("Persona created");
      onCreated();
    } catch (e) {
      toast(formatApiError("Create persona", e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer open onClose={onClose}>
      <div className="eyebrow"><span>New persona</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>Create persona</h2>
      <TextField label="Name" value={name} onChange={setName} placeholder="atelier" />
      <CascadeFields
        runtimes={runtimes.data ?? []}
        runtimeId={runtimeId}
        setRuntimeId={setRuntimeId}
        model={model}
        setModel={setModel}
      />
      <TextField label="Color" value={color} onChange={setColor} placeholder="#8b5cf6" />
      <TextArea label="Instructions" value={prompt} onChange={setPrompt} />
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
