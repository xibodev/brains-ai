import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, formatApiError } from "../api/client";
import type { OnboardingAttempt, OnboardingState, Runtime } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { Stepper } from "../components/Stepper";
import { SoftCard } from "../components/SoftCard";
import { TextField, Select } from "../components/Field";
import { ConnectMachineModal } from "../components/ConnectMachineModal";
import { useToast } from "../components/Toast";
import { blockedCopy } from "../components/blockedReason";

// Fresh-state onboarding (F6 / BL-P1-04).
//
// The attempt lives on the server, so a reload, a second tab and a machine that
// never connects all resume the same run. Nothing here seeds an Org, Persona,
// Project, Issue or Session: every entity is created by an explicit product API
// call and the outcome is recorded, which is why the flow can end "blocked with
// a reason" and can never claim a success it did not produce.

const STEP_RAIL: Record<string, { title: string; body: string }> = {
  org: {
    title: "Organisation",
    body: "Create an org to scope all your work — personas, pods, issues, and sessions live under an org.",
  },
  runtime: {
    title: "Connect a machine",
    body: "Run the brains daemon on any machine so your personas have somewhere to execute. You can defer this, and onboarding will say so rather than pretend the work ran.",
  },
  persona: {
    title: "Create a persona",
    body: "A persona is an AI operator identity. Pick a runtime to bind it to a model and tool, then give it a name.",
  },
  work: {
    title: "First issue",
    body: "Issues are units of work. Create a project and a first task — it will be assigned to your new persona.",
  },
  dispatch: {
    title: "Dispatch",
    body: "Dispatching spawns a real session and hands the issue to your persona. Onboarding completes only when that session exists.",
  },
  done: {
    title: "All set",
    body: "Your workspace is ready. Head to the board to create more issues, or to Personas to add more operators.",
  },
};

const STEP_INDEX: Record<string, number> = {
  org: 1,
  runtime: 2,
  persona: 3,
  work: 4,
  dispatch: 5,
  done: 5,
};

export function Onboarding() {
  const navigate = useNavigate();
  const { refresh, setActiveOrg, activeOrg } = useOrg();
  const { toast } = useToast();

  const [state, setState] = useState<OnboardingState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // step inputs
  const [orgSlug, setOrgSlug] = useState("");
  const [orgName, setOrgName] = useState("");
  const [connectOpen, setConnectOpen] = useState(false);
  const [personaName, setPersonaName] = useState("atelier");
  const [model, setModel] = useState("");
  const [runtimeId, setRuntimeId] = useState("");
  const [runtimes, setRuntimes] = useState<Runtime[]>([]);
  const [projectName, setProjectName] = useState("Brains project");
  const [issueTitle, setIssueTitle] = useState("Scaffold the SPA");
  const [priority, setPriority] = useState("p1");

  const attempt: OnboardingAttempt | null = state?.attempt ?? null;
  const step = attempt?.current_step ?? "org";

  const load = () => {
    setLoadError(null);
    api
      .onboardingState()
      .then(async (next) => {
        // On a working install there is no open attempt until the operator
        // asks for one; opening this screen is that request.
        if (next.attempt === null) return api.startOnboarding();
        return next;
      })
      .then(setState)
      .catch((e) => setLoadError(formatApiError("Load onboarding", e)));
  };

  // Resume on mount — and therefore on every browser refresh.
  useEffect(load, []);

  useEffect(() => {
    if (step !== "runtime" && step !== "persona") return;
    api.listRuntimes().then(setRuntimes).catch(() => undefined);
  }, [step]);

  const selectedRuntime = runtimes.find((r) => String(r.id) === runtimeId);
  const runtimeModels: string[] = selectedRuntime?.capabilities?.models ?? [];
  const modelOptions = [...new Set([...(model ? [model] : []), ...runtimeModels])];
  const effectiveOrg = attempt?.entities.org?.slug ?? activeOrg?.slug ?? orgSlug;

  const record = async (
    stepName: string,
    body: Parameters<typeof api.recordOnboardingStep>[2],
  ) => {
    if (!attempt) return;
    const next = await api.recordOnboardingStep(attempt.attempt_id, stepName, body);
    setState((prev) => (prev ? { ...prev, attempt: next } : prev));
  };

  const guarded = async (label: string, action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } catch (e) {
      toast(formatApiError(label, e));
    } finally {
      setBusy(false);
    }
  };

  const createOrg = () =>
    guarded("Create org", async () => {
      if (!orgSlug.trim() || !orgName.trim()) {
        toast("Slug and name required");
        return;
      }
      const org = await api.createOrg({ slug: orgSlug.trim(), name: orgName.trim() });
      setActiveOrg(orgSlug.trim());
      refresh();
      await record("org", { status: "done", org_id: Number(org.id), entity_ref: org.slug });
    });

  const useExistingOrg = () =>
    guarded("Use org", async () => {
      if (!activeOrg) return;
      await record("org", {
        status: "done",
        org_id: Number(activeOrg.id),
        entity_ref: activeOrg.slug,
      });
    });

  const deferRuntime = () =>
    guarded("Defer machine", async () => {
      await record("runtime", {
        status: "deferred",
        detail: "Machine setup deferred during onboarding.",
      });
    });

  const runtimeConnected = () =>
    guarded("Connect machine", async () => {
      setConnectOpen(false);
      const list = await api.listRuntimes().catch(() => [] as Runtime[]);
      setRuntimes(list);
      const online = list.find((r) => r.status === "online") ?? list[0];
      if (!online) {
        await record("runtime", {
          status: "failed",
          error: "No runtime registered yet — the daemon has not checked in.",
        });
        return;
      }
      await record("runtime", {
        status: "done",
        runtime_id: Number(online.id),
        entity_ref: online.slug,
      });
    });

  const createPersona = () =>
    guarded("Create persona", async () => {
      if (!personaName.trim()) {
        toast("Persona name required");
        return;
      }
      const persona = await api.createPersona(effectiveOrg, {
        slug: personaName.toLowerCase().replace(/\s+/g, "-"),
        name: personaName,
        ...(model ? { model } : {}),
        ...(selectedRuntime?.tool ? { tool: selectedRuntime.tool } : {}),
        ...(runtimeId ? { default_runtime_id: runtimeId } : {}),
      });
      await record("persona", {
        status: "done",
        persona_id: Number(persona.id),
        entity_ref: persona.slug,
        ...(runtimeId ? { runtime_id: Number(runtimeId) } : {}),
      });
    });

  const createWork = () =>
    guarded("Create project", async () => {
      const project = await api.createProject(effectiveOrg, {
        slug: projectName.toLowerCase().replace(/\s+/g, "-"),
        name: projectName,
      });
      const issue = await api.createIssue(project.code, {
        title: issueTitle,
        priority: priority as never,
      });
      if (attempt?.persona_id) {
        await api.assignIssue(issue.code, { persona_id: String(attempt.persona_id) });
      }
      await record("work", {
        status: "done",
        project_id: Number(project.id),
        issue_id: Number(issue.id),
        entity_ref: issue.code,
      });
    });

  const dispatch = () =>
    guarded("Dispatch", async () => {
      const issueCode = attempt?.entities.issue?.code;
      if (!issueCode) {
        toast("No issue to dispatch yet");
        return;
      }
      try {
        const result = await api.dispatchIssue(issueCode);
        await record("dispatch", { status: "done", session_id: result.session_id });
      } catch (e) {
        await record("dispatch", { status: "failed", error: formatApiError("Dispatch", e) });
      }
    });

  const leave = () =>
    guarded("Leave onboarding", async () => {
      if (attempt) await api.abandonOnboarding(attempt.attempt_id);
      navigate("/issues");
    });

  const rail = STEP_RAIL[step] ?? STEP_RAIL.done;
  const stepperCurrent = STEP_INDEX[step] ?? 5;
  const blocked = attempt?.status === "blocked";
  const completed = attempt?.status === "completed";

  if (loadError) {
    return (
      <div className="onboard-panel" style={{ padding: 32, maxWidth: 520 }}>
        <SoftCard>
          <h1>Onboarding could not load.</h1>
          <p className="meta">{loadError}</p>
          <button className="btn primary" onClick={load}>
            Retry
          </button>
        </SoftCard>
      </div>
    );
  }

  return (
    <div
      className="onboard"
      style={{ display: "flex", minHeight: "100vh", alignItems: "flex-start" }}
    >
      <div className="onboard-panel" style={{ flex: "1 1 0", maxWidth: 520 }}>
        {!completed && !blocked && <Stepper total={5} current={stepperCurrent} />}

        {attempt === null && <SoftCard style={{ marginTop: 24 }}>Loading…</SoftCard>}

        {attempt && completed && (
          <SoftCard style={{ marginTop: 24 }} data-testid="onboarding-complete">
            <div className="eyebrow"><span>Complete</span></div>
            <h1>You're set up.</h1>
            <p className="meta">
              Session <strong>{attempt.entities.session?.id}</strong> is running{" "}
              <strong>{attempt.entities.issue?.code}</strong>.
            </p>
            <div className="row" style={{ marginTop: 16 }}>
              <button
                className="btn primary"
                onClick={() => {
                  refresh();
                  navigate("/issues");
                }}
              >
                Open the board ▷
              </button>
            </div>
          </SoftCard>
        )}

        {attempt && blocked && (
          <SoftCard style={{ marginTop: 24 }} data-testid="onboarding-blocked">
            <div className="eyebrow"><span>Blocked</span></div>
            <h1>Onboarding stopped short of a session.</h1>
            <p className="meta">{blockedCopy(attempt.blocked_reason)}</p>
            {attempt.recovery && <p className="meta">{attempt.recovery.detail}</p>}
            <div className="row" style={{ marginTop: 16 }}>
              {attempt.recovery && (
                <button
                  className="btn primary"
                  onClick={() => navigate(attempt.recovery!.route)}
                  data-testid="onboarding-recovery"
                >
                  {attempt.recovery.label} ▷
                </button>
              )}
              <button className="btn" disabled={busy} onClick={() => void dispatch()}>
                Retry dispatch
              </button>
              <button className="btn ghost" onClick={load}>
                Refresh
              </button>
            </div>
          </SoftCard>
        )}

        {attempt && !completed && !blocked && step === "org" && (
          <SoftCard style={{ marginTop: 24 }}>
            <h1>Name your organisation.</h1>
            <TextField label="Slug" value={orgSlug} onChange={setOrgSlug} placeholder="acme" />
            <TextField label="Name" value={orgName} onChange={setOrgName} placeholder="Acme" />
            {activeOrg && (
              <div className="meta" style={{ marginBottom: 8 }}>
                Active org: <strong>{activeOrg.slug}</strong> —{" "}
                <button className="btn ghost small" disabled={busy} onClick={() => void useExistingOrg()}>
                  use it
                </button>
              </div>
            )}
            <button className="btn primary" disabled={busy} onClick={() => void createOrg()}>
              Continue ▷
            </button>
          </SoftCard>
        )}

        {attempt && !completed && !blocked && step === "runtime" && (
          <SoftCard style={{ marginTop: 24 }}>
            <h1>Connect a machine.</h1>
            <p className="meta">
              Run the brains daemon on a host to give personas a runtime. You can defer this —
              onboarding will then end in an explicit blocked state instead of claiming a
              session ran.
            </p>
            <div className="row" style={{ marginTop: 16 }}>
              <button className="btn primary" onClick={() => setConnectOpen(true)}>
                Connect a machine ▷
              </button>
              <button className="btn ghost" disabled={busy} onClick={() => void deferRuntime()}>
                Defer for now
              </button>
            </div>
            <ConnectMachineModal
              open={connectOpen}
              onClose={() => setConnectOpen(false)}
              onConnected={() => void runtimeConnected()}
            />
          </SoftCard>
        )}

        {attempt && !completed && !blocked && step === "persona" && (
          <SoftCard style={{ marginTop: 24 }}>
            <h1>Create your first persona.</h1>
            <Select
              label="Runtime"
              value={runtimeId}
              onChange={(v) => {
                setRuntimeId(v);
                setModel("");
              }}
              options={[
                { value: "", label: "— no runtime (local) —" },
                ...runtimes
                  .filter((r) => r.status === "online")
                  .map((r) => ({
                    value: String(r.id),
                    label: `${r.machine_label ?? r.slug} (${r.tool ?? "?"})`,
                  })),
              ]}
            />
            {modelOptions.length > 0 ? (
              <Select
                label="Model"
                value={model}
                onChange={setModel}
                options={modelOptions.map((m) => ({ value: m, label: m }))}
              />
            ) : (
              <Select
                label="Model"
                value={model}
                onChange={setModel}
                options={[
                  { value: "", label: "— default —" },
                  ...["gpt-5", "claude-sonnet", "llama3"].map((m) => ({ value: m, label: m })),
                ]}
              />
            )}
            <TextField label="Persona name" value={personaName} onChange={setPersonaName} />
            <button className="btn primary" disabled={busy} onClick={() => void createPersona()}>
              Continue ▷
            </button>
          </SoftCard>
        )}

        {attempt && !completed && !blocked && step === "work" && (
          <SoftCard style={{ marginTop: 24 }}>
            <h1>Create a project + first issue.</h1>
            <p className="meta" style={{ marginBottom: 12 }}>
              This issue will be assigned to <strong>{personaName}</strong>.
            </p>
            <TextField label="Project name" value={projectName} onChange={setProjectName} />
            <TextField label="Issue title" value={issueTitle} onChange={setIssueTitle} />
            <Select
              label="Priority"
              value={priority}
              onChange={setPriority}
              options={["p0", "p1", "p2", "p3"].map((p) => ({ value: p, label: p }))}
            />
            <button className="btn primary" disabled={busy} onClick={() => void createWork()}>
              Continue ▷
            </button>
          </SoftCard>
        )}

        {attempt && !completed && !blocked && step === "dispatch" && (
          <SoftCard style={{ marginTop: 24 }}>
            <h1>Dispatch to your persona.</h1>
            <p className="meta" style={{ marginBottom: 16 }}>
              This spawns a real session for{" "}
              <strong>{attempt.entities.issue?.code ?? "the issue"}</strong>. Onboarding is
              complete only once that session exists.
            </p>
            <div className="row">
              <button className="btn primary" disabled={busy} onClick={() => void dispatch()}>
                Dispatch ▷
              </button>
              <button className="btn ghost" disabled={busy} onClick={() => void leave()}>
                Not now
              </button>
            </div>
          </SoftCard>
        )}

        {attempt && (
          <div className="meta" style={{ marginTop: 20 }} data-testid="onboarding-steps">
            {attempt.steps
              .map((s) => `${s.step}: ${s.status}${s.attempts > 1 ? ` (${s.attempts} tries)` : ""}`)
              .join(" · ")}
          </div>
        )}
      </div>

      <aside
        style={{
          flex: "0 0 260px",
          padding: "48px 24px 32px",
          borderLeft: "1px solid var(--border)",
          alignSelf: "stretch",
        }}
      >
        <div className="eyebrow"><span>Step {stepperCurrent} / 5</span></div>
        <h3 style={{ margin: "8px 0 12px", fontSize: 16 }}>{rail.title}</h3>
        <p className="meta" style={{ lineHeight: 1.6 }}>{rail.body}</p>
      </aside>
    </div>
  );
}
