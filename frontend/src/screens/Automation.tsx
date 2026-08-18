import { useState } from "react";
import { api, formatApiError } from "../api/client";
import type { Autopilot, Skill } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { useAsync } from "../store/useAsync";
import { ScreenHead } from "./ScreenHead";
import { SoftCard } from "../components/SoftCard";
import { Loading, EmptyState } from "../components/EmptyState";
import { Drawer } from "../components/Drawer";
import { StatusPill } from "../components/StatusPill";
import { TextField, TextArea } from "../components/Field";
import { Toggle } from "../components/Field";
import { useToast } from "../components/Toast";

export function Automation() {
  const { activeOrg } = useOrg();
  const { toast } = useToast();

  const [creatingAutopilot, setCreatingAutopilot] = useState(false);
  const [creatingSkill, setCreatingSkill] = useState(false);

  const autopilots = useAsync<Autopilot[]>(
    () => (activeOrg ? api.listAutopilots(activeOrg.slug).catch(() => []) : Promise.resolve([])),
    [activeOrg?.slug],
  );
  const skills = useAsync<Skill[]>(
    () => (activeOrg ? api.listSkills(activeOrg.slug).catch(() => []) : Promise.resolve([])),
    [activeOrg?.slug],
  );

  const handleToggleEnabled = async (ap: Autopilot) => {
    try {
      await api.setAutopilotEnabled(ap.name, !ap.enabled);
      toast(`Autopilot ${ap.enabled ? "disabled" : "enabled"}`);
      autopilots.refetch();
    } catch (e) {
      toast(formatApiError("Toggle autopilot", e));
    }
  };

  const handleFire = async (ap: Autopilot) => {
    try {
      await api.fireAutopilot(ap.name);
      toast(`Autopilot "${ap.name}" fired`);
    } catch (e) {
      toast(formatApiError("Fire autopilot", e));
    }
  };

  return (
    <div>
      <ScreenHead
        eyebrow="Automation"
        title="Autopilots & Skills"
        actions={
          <div className="row" style={{ gap: 8 }}>
            <button className="btn primary" onClick={() => setCreatingAutopilot(true)}>
              + New autopilot
            </button>
            <button className="btn" onClick={() => setCreatingSkill(true)}>
              + New skill
            </button>
          </div>
        }
      />

      {/* Autopilots section */}
      <div style={{ marginBottom: 32 }}>
        <div className="eyebrow" style={{ marginBottom: 8 }}>
          <span>Autopilots</span>
        </div>

        {autopilots.loading && autopilots.data === undefined ? (
          <Loading />
        ) : (autopilots.data ?? []).length === 0 ? (
          <EmptyState
            title="No autopilots yet"
            body="Recurring agent tasks that fire on a schedule or on demand."
            action={
              <button className="btn primary" onClick={() => setCreatingAutopilot(true)}>
                + New autopilot
              </button>
            }
          />
        ) : (
          <div className="grid" data-testid="autopilots-list">
            {(autopilots.data ?? []).map((ap) => (
              <SoftCard key={ap.name}>
                <div className="row spread">
                  <div>
                    <strong>{ap.name}</strong>
                    <div className="meta" style={{ marginTop: 2 }}>
                      {ap.cron_expr ?? "manual"}
                    </div>
                  </div>
                  <div className="row" style={{ gap: 8 }}>
                    <StatusPill label={ap.enabled ? "enabled" : "disabled"} tone={ap.enabled ? "positive" : "neutral"} />
                    <Toggle
                      checked={ap.enabled}
                      onChange={() => void handleToggleEnabled(ap)}
                    />
                    <button
                      className="btn small"
                      onClick={() => void handleFire(ap)}
                    >
                      Fire
                    </button>
                  </div>
                </div>
                {ap.title_template && (
                  <div className="meta" style={{ marginTop: 6 }}>
                    {ap.title_template}
                  </div>
                )}
              </SoftCard>
            ))}
          </div>
        )}
      </div>

      {/* Skills section */}
      <div>
        <div className="eyebrow" style={{ marginBottom: 8 }}>
          <span>Skills</span>
        </div>

        {skills.loading && skills.data === undefined ? (
          <Loading />
        ) : (skills.data ?? []).length === 0 ? (
          <EmptyState
            title="No skills yet"
            body="SKILL.md packs that extend agent capabilities."
            action={
              <button className="btn primary" onClick={() => setCreatingSkill(true)}>
                + New skill
              </button>
            }
          />
        ) : (
          <div className="grid" data-testid="skills-list">
            {(skills.data ?? []).map((sk) => (
              <SoftCard key={String(sk.id)}>
                <div className="row spread">
                  <strong>{sk.name}</strong>
                  <span className="meta">{sk.slug}</span>
                </div>
              </SoftCard>
            ))}
          </div>
        )}
      </div>

      {creatingAutopilot && activeOrg && (
        <CreateAutopilotDrawer
          orgSlug={activeOrg.slug}
          onClose={() => setCreatingAutopilot(false)}
          onCreated={() => {
            setCreatingAutopilot(false);
            autopilots.refetch();
            toast("Autopilot created");
          }}
        />
      )}

      {creatingSkill && activeOrg && (
        <CreateSkillDrawer
          orgSlug={activeOrg.slug}
          onClose={() => setCreatingSkill(false)}
          onCreated={() => {
            setCreatingSkill(false);
            skills.refetch();
            toast("Skill created");
          }}
        />
      )}
    </div>
  );
}

// Supported schedule grammar (mirrors brains.control.recurring.is_valid_schedule):
// manual | hourly | daily | every:<N><s|m|h|d>. General cron syntax is not
// supported — validated client-side so a typo is caught before the request
// round-trips, and matched server-side regardless.
const SCHEDULE_EVERY = /^every:(\d+)[smhd]$/i;

function isValidSchedule(value: string): boolean {
  const v = value.trim().toLowerCase();
  if (v === "manual" || v === "hourly" || v === "daily") return true;
  const match = SCHEDULE_EVERY.exec(v);
  return !!match && Number(match[1]) > 0;
}

function CreateAutopilotDrawer({
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
  const [titleTemplate, setTitleTemplate] = useState("");
  const [cronExpr, setCronExpr] = useState("manual");
  const [spawnTool, setSpawnTool] = useState("");
  const [spawnPrompt, setSpawnPrompt] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim() || !titleTemplate.trim()) {
      toast("Name and title template are required");
      return;
    }
    const schedule = cronExpr.trim() || "manual";
    if (!isValidSchedule(schedule)) {
      toast(
        `"${schedule}" isn't a supported schedule. Use manual, hourly, daily, or every:<N><s|m|h|d> ` +
          "(e.g. every:15m) — cron syntax is not supported.",
      );
      return;
    }
    setBusy(true);
    try {
      await api.createAutopilot(orgSlug, {
        name: name.trim(),
        title_template: titleTemplate.trim(),
        cron_expr: schedule,
        spawn_tool: spawnTool.trim() || undefined,
        spawn_prompt: spawnPrompt.trim() || undefined,
      });
      onCreated();
    } catch (e) {
      toast(formatApiError("Create autopilot", e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer open onClose={onClose}>
      <div className="eyebrow"><span>New autopilot</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>Create autopilot</h2>
      <TextField label="Name" value={name} onChange={setName} placeholder="daily-standup" />
      <TextField
        label="Title template"
        value={titleTemplate}
        onChange={setTitleTemplate}
        placeholder="Daily standup {date}"
      />
      <TextField
        label="Schedule"
        value={cronExpr}
        onChange={setCronExpr}
        placeholder="manual, hourly, daily, or every:15m"
      />
      <div className="meta" style={{ marginTop: -8, marginBottom: 12 }}>
        Supported: <code>manual</code>, <code>hourly</code>, <code>daily</code>, or{" "}
        <code>every:&lt;N&gt;&lt;s|m|h|d&gt;</code> (e.g. <code>every:15m</code>). General cron
        syntax is not supported.
      </div>
      <TextField
        label="Spawn tool (optional)"
        value={spawnTool}
        onChange={setSpawnTool}
        placeholder="copilot-cli"
      />
      <TextArea
        label="Spawn prompt (optional)"
        value={spawnPrompt}
        onChange={setSpawnPrompt}
        placeholder="Run the daily standup..."
      />
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

function CreateSkillDrawer({
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
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);

  const derivedSlug =
    slugOverride || name.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "");

  const submit = async () => {
    if (!name.trim()) {
      toast("Name is required");
      return;
    }
    setBusy(true);
    try {
      await api.createSkill(orgSlug, {
        slug: derivedSlug || name.toLowerCase(),
        name: name.trim(),
        content: content.trim() || undefined,
      });
      onCreated();
    } catch (e) {
      toast(formatApiError("Create skill", e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer open onClose={onClose}>
      <div className="eyebrow"><span>New skill</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>Create skill</h2>
      <TextField label="Name" value={name} onChange={setName} placeholder="My skill" />
      <TextField
        label="Slug (auto-derived)"
        value={slugOverride}
        onChange={setSlugOverride}
        placeholder={derivedSlug || "my-skill"}
      />
      <TextArea
        label="Content (SKILL.md body)"
        value={content}
        onChange={setContent}
        placeholder="# My skill&#10;&#10;Describe what this skill does..."
      />
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
