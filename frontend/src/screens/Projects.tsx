import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Issue, Project, SkillAttachment, Workspace } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { useAsync } from "../store/useAsync";
import { useTopic } from "../realtime/useRealtime";
import { ScreenHead } from "./ScreenHead";
import { SoftCard } from "../components/SoftCard";
import { StatusPill } from "../components/StatusPill";
import { AsyncBoundary } from "../components/EmptyState";
import { Drawer } from "../components/Drawer";
import { Board, BOARD_COLUMNS } from "../components/Board";
import { TextField, TextArea, Select } from "../components/Field";
import { SkillAttachmentPanel } from "../components/SkillAttachmentPanel";
import { useToast } from "../components/Toast";

// Projects — list → detail with the project's embedded issue board (WS4 §3.5).
export function Projects() {
  const { activeOrg } = useOrg();
  const navigate = useNavigate();
  const { code: routeCode } = useParams();
  const [selected, setSelected] = useState<Project | null>(null);
  const [creating, setCreating] = useState(false);

  const state = useAsync<Project[]>(
    () => (activeOrg ? api.listProjects(activeOrg.slug) : Promise.resolve([])),
    [activeOrg?.slug],
  );
  const workspaces = useAsync<Workspace[]>(
    () => (activeOrg ? api.listWorkspaces(activeOrg.slug) : Promise.resolve([])),
    [activeOrg?.slug],
  );
  useTopic(activeOrg ? `org/${activeOrg.slug}/projects` : null, () => state.refetch());

  const [deepLinkMissing, setDeepLinkMissing] = useState(false);
  useEffect(() => {
    if (!routeCode || state.data === undefined) {
      setDeepLinkMissing(false);
      return;
    }
    const match = (state.data ?? []).find(
      (project) => project.code === routeCode || project.slug === routeCode,
    );
    setDeepLinkMissing(!match);
    if (match) setSelected(match);
  }, [routeCode, state.data]);

  const close = () => {
    setSelected(null);
    if (routeCode) navigate("/projects");
  };

  return (
    <div>
      <ScreenHead
        eyebrow="Projects"
        title="Work containers"
        actions={
          <button className="btn primary" onClick={() => setCreating(true)}>
            + New project
          </button>
        }
      />
      {deepLinkMissing && (
        <SoftCard>
          <div className="meta" data-testid="project-not-found">
            No project named <strong>{routeCode}</strong> in this org.
          </div>
        </SoftCard>
      )}
      <AsyncBoundary
        state={state}
        emptyTitle="No projects yet"
        emptyBody="A project is the container above issues. Create one to start a board."
        emptyAction={
          <button className="btn primary" onClick={() => setCreating(true)}>
            + New project
          </button>
        }
      >
        {(projects) => (
          <div className="grid">
            {projects.map((p) => (
              <SoftCard
                key={String(p.id)}
                interactive
                onClick={() => navigate(`/projects/${p.code}`)}
              >
                <div className="row spread">
                  <strong>{p.name}</strong>
                  <StatusPill label={p.status ?? "active"} />
                </div>
                <div className="meta" style={{ marginTop: 6 }}>
                  <span className="mono">{p.code}</span>
                </div>
              </SoftCard>
            ))}
          </div>
        )}
      </AsyncBoundary>

      <Drawer open={!!selected} onClose={close}>
        {selected && <ProjectDetail project={selected} workspaces={workspaces.data ?? []} />}
      </Drawer>

      {creating && (
        <CreateProject
          workspaces={workspaces.data ?? []}
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

function ProjectDetail({
  project,
  workspaces,
}: {
  project: Project;
  workspaces: Workspace[];
}) {
  const { toast } = useToast();
  const { activeOrg } = useOrg();
  const issues = useAsync<Issue[]>(
    () => api.projectIssues(project.code).catch(() => []),
    [project.code],
  );
  const projectSkills = useAsync<SkillAttachment[]>(
    () => api.listProjectSkills(project.code).catch(() => []),
    [project.code],
  );

  const move = async (issue: Issue, status: Issue["status"]) => {
    issues.setData((prev) => (prev ?? []).map((i) => (i.code === issue.code ? { ...i, status } : i)));
    try {
      await api.transitionIssue(issue.code, status);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Move failed");
      issues.refetch();
    }
  };

  return (
    <div style={{ width: "100%" }}>
      <div className="eyebrow"><span>{project.code}</span></div>
      <h2 style={{ margin: "8px 0 12px" }}>{project.name}</h2>
      {project.description && <p className="meta">{project.description}</p>}
      <div className="meta" style={{ marginTop: 8 }} data-testid="project-workspace">
        Workspace:{" "}
        <strong>
          {workspaces.find((workspace) => String(workspace.id) === String(project.workspace_id))
            ?.name ??
            workspaces.find((workspace) => String(workspace.id) === String(project.workspace_id))
              ?.slug ??
            "Not linked"}
        </strong>
      </div>
      <div className="eyebrow" style={{ margin: "16px 0 8px" }}><span>Issues</span></div>
      {(issues.data ?? []).length === 0 ? (
        <div className="meta">No issues in this project yet.</div>
      ) : (
        <Board
          issues={issues.data ?? []}
          columns={BOARD_COLUMNS}
          onOpen={() => undefined}
          onMove={move}
          compact
        />
      )}
      <div style={{ marginTop: 20 }}>
        <SkillAttachmentPanel
          orgSlug={activeOrg?.slug}
          attached={projectSkills}
          onAttach={(skillId) => api.attachProjectSkill(project.code, skillId)}
          onDetach={(skillId) => api.detachProjectSkill(project.code, skillId)}
        />
      </div>
    </div>
  );
}

function CreateProject({
  workspaces,
  onClose,
  onCreated,
}: {
  workspaces: Workspace[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const { activeOrg } = useOrg();
  const { toast } = useToast();
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!activeOrg || !name.trim()) {
      toast("Name required");
      return;
    }
    setBusy(true);
    try {
      await api.createProject(activeOrg.slug, {
        slug: name.trim().toLowerCase().replace(/\s+/g, "-"),
        name: name.trim(),
        description: desc,
        ...(workspaceId ? { workspace_id: workspaceId } : {}),
      });
      toast("Project created");
      onCreated();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Create failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer open onClose={onClose}>
      <div className="eyebrow"><span>New project</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>Create project</h2>
      <TextField label="Name" value={name} onChange={setName} placeholder="Brains project" />
      <TextArea label="Description" value={desc} onChange={setDesc} />
      <Select
        label="Workspace"
        value={workspaceId}
        onChange={setWorkspaceId}
        options={[
          { value: "", label: "— not linked —" },
          ...workspaces.map((workspace) => ({
            value: String(workspace.id),
            label: `${workspace.name ?? workspace.slug} — ${workspace.path}`,
          })),
        ]}
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
