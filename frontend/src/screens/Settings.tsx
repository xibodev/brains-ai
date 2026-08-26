import { useNavigate, useParams } from "react-router-dom";
import { api, formatApiError } from "../api/client";
import { useOrg } from "../store/OrgContext";
import { useAsync } from "../store/useAsync";
import { ScreenHead } from "./ScreenHead";
import { MasterDetail, type RailItem } from "../components/MasterDetail";
import { SoftCard } from "../components/SoftCard";
import { StatusPill } from "../components/StatusPill";
import { TextField, Select } from "../components/Field";
import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";

// Settings — same master-detail pattern as Config (WS4 §3.9).
const SECTIONS: RailItem[] = [
  { key: "org", label: "Org", section: "Settings" },
  { key: "members", label: "Members", section: "Settings" },
  { key: "usage", label: "Usage", section: "Settings" },
  { key: "operator", label: "Operator", section: "Settings" },
  { key: "about", label: "About", section: "Settings" },
];

export function Settings() {
  const { section = "org" } = useParams();
  const navigate = useNavigate();

  return (
    <div style={{ height: "100%" }}>
      <ScreenHead eyebrow={`Settings ▸ ${labelFor(section)}`} title="Settings" />
      <MasterDetail
        items={SECTIONS}
        activeKey={section}
        onSelect={(k) => navigate(`/operations/access/${k}`)}
        railOnLeft
      >
        {section === "org" && <OrgSettings />}
        {section === "members" && <Members />}
        {section === "usage" && <Usage />}
        {section === "operator" && <OperatorSettings />}
        {section === "about" && <About />}
      </MasterDetail>
    </div>
  );
}

function labelFor(key: string): string {
  return SECTIONS.find((s) => s.key === key)?.label ?? key;
}

function OrgSettings() {
  const { activeOrg, refresh } = useOrg();
  const { toast } = useToast();
  const [name, setName] = useState(activeOrg?.name ?? "");
  const [desc, setDesc] = useState(activeOrg?.description ?? "");

  // activeOrg can resolve after first render (and changes when the org switches);
  // sync the form fields so the inputs reflect the current org rather than the
  // empty initial state.
  useEffect(() => {
    setName(activeOrg?.name ?? "");
    setDesc(activeOrg?.description ?? "");
  }, [activeOrg?.slug, activeOrg?.name, activeOrg?.description]);

  if (!activeOrg) return <div className="meta">No active org.</div>;

  const save = async () => {
    try {
      await api.patchOrg(activeOrg.slug, { name, description: desc });
      toast("Org saved");
      refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Save failed");
    }
  };

  return (
    <div>
      <div className="eyebrow"><span>Organisation</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>{activeOrg.name}</h2>
      <TextField label="Name" value={name} onChange={setName} />
      <TextField label="Description" value={desc} onChange={setDesc} />
      <div className="row wrap" style={{ marginBottom: 16 }}>
        <span className="meta">slug</span>
        <span className="mono">{activeOrg.slug}</span>
        <StatusPill label={activeOrg.status ?? "active"} />
      </div>
      <button className="btn primary" onClick={() => void save()}>Save</button>
    </div>
  );
}

const ROLE_OPTIONS = [
  { value: "member", label: "Member" },
  { value: "admin", label: "Admin" },
  { value: "owner", label: "Owner" },
];

function Members() {
  const { activeOrg } = useOrg();
  const { toast } = useToast();
  const members = useAsync(
    () => (activeOrg ? api.listOrgMembers(activeOrg.slug) : Promise.resolve([])),
    [activeOrg?.slug],
  );

  const [inviteSlug, setInviteSlug] = useState("");
  const [inviteRole, setInviteRole] = useState("member");
  const [adding, setAdding] = useState(false);

  const handleAdd = async () => {
    if (!activeOrg || !inviteSlug.trim()) return;
    setAdding(true);
    try {
      await api.addOrgMember(activeOrg.slug, { operator_id: inviteSlug.trim(), role: inviteRole });
      toast(`Added ${inviteSlug.trim()}`);
      setInviteSlug("");
      setInviteRole("member");
      members.refetch();
    } catch (e) {
      const msg = formatApiError("Add member", e);
      // The server refuses an operator slug it has never seen (no invitation
      // flow exists yet — adding a member requires an operator that already
      // exists). Say so plainly instead of leaving a raw 404/400 on screen.
      if (/unknown operator/i.test(msg)) {
        toast(
          `No operator named "${inviteSlug.trim()}" exists yet. Adding a member requires an ` +
            "existing operator — create/enrol one first, then add it here.",
        );
      } else {
        toast(msg);
      }
    } finally {
      setAdding(false);
    }
  };

  const handleRemove = async (operatorSlug: string) => {
    if (!activeOrg) return;
    try {
      await api.removeOrgMember(activeOrg.slug, operatorSlug);
      toast(`Removed ${operatorSlug}`);
      members.refetch();
    } catch (e) {
      toast(formatApiError("Remove member", e));
    }
  };

  return (
    <div>
      <div className="eyebrow"><span>Members</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>Org members</h2>
      <p className="meta" style={{ marginBottom: 16 }}>
        Adding a member requires an operator that already exists on this install — there is no
        email/invitation flow yet. Name an existing operator's slug below.
      </p>

      <SoftCard style={{ marginBottom: 16 }}>
        <div className="eyebrow"><span>Invite</span></div>
        <TextField
          label="Operator slug"
          value={inviteSlug}
          onChange={setInviteSlug}
          placeholder="e.g. alice"
        />
        <Select
          label="Role"
          value={inviteRole}
          onChange={setInviteRole}
          options={ROLE_OPTIONS}
        />
        <button
          className="btn primary"
          style={{ marginTop: 8 }}
          disabled={adding || !inviteSlug.trim()}
          onClick={() => void handleAdd()}
        >
          Add
        </button>
      </SoftCard>

      {(members.data ?? []).length === 0 ? (
        <div className="meta">No members loaded.</div>
      ) : (
        <div className="card-list">
          {(members.data ?? []).map((m) => (
            <SoftCard key={String(m.operator)}>
              <div className="row spread">
                <span>{m.name ?? m.operator}</span>
                <div className="row" style={{ gap: 8 }}>
                  <StatusPill label={m.role} tone="accent" />
                  <button
                    className="btn"
                    style={{ fontSize: "0.75rem", padding: "2px 8px" }}
                    onClick={() => void handleRemove(m.operator)}
                  >
                    Remove
                  </button>
                </div>
              </div>
            </SoftCard>
          ))}
        </div>
      )}
    </div>
  );
}

function Usage() {
  const { activeOrg } = useOrg();
  const usage = useAsync(
    () => (activeOrg ? api.orgUsageSummary(activeOrg.slug, 30) : Promise.resolve(undefined)),
    [activeOrg?.slug],
  );

  if (!activeOrg) return <div className="meta">No active org — select one to see its usage.</div>;
  if (usage.loading && usage.data === undefined) return <div className="meta">Loading usage&hellip;</div>;
  if (usage.error) return <div className="meta">Failed to load usage: {usage.error}</div>;

  const u = usage.data;
  const noData = !u || (u.totals.calls === 0 && u.top_models.length === 0);

  return (
    <div data-testid="usage-summary">
      <div className="eyebrow"><span>Usage · {activeOrg.name}</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>Last {u?.days ?? 30} days</h2>
      <p className="meta" style={{ marginBottom: 16 }}>
        Scoped to gateway calls this Org's own Sessions made — never another Org's activity.
      </p>

      {noData ? (
        <div className="meta">No gateway usage recorded yet for this Org.</div>
      ) : (
        <>
          <div className="row wrap" style={{ gap: 12, marginBottom: 20 }}>
            <SoftCard style={{ minWidth: 120, textAlign: "center" }}>
              <div className="eyebrow"><span>Calls</span></div>
              <div style={{ fontSize: "1.5rem", fontWeight: 600 }}>{u!.totals.calls}</div>
            </SoftCard>
            <SoftCard style={{ minWidth: 120, textAlign: "center" }}>
              <div className="eyebrow"><span>Input tokens</span></div>
              <div style={{ fontSize: "1.5rem", fontWeight: 600 }}>{u!.totals.input_tokens.toLocaleString()}</div>
            </SoftCard>
            <SoftCard style={{ minWidth: 120, textAlign: "center" }}>
              <div className="eyebrow"><span>Output tokens</span></div>
              <div style={{ fontSize: "1.5rem", fontWeight: 600 }}>{u!.totals.output_tokens.toLocaleString()}</div>
            </SoftCard>
            {u!.totals.cost_actual_usd != null && (
              <SoftCard style={{ minWidth: 120, textAlign: "center" }}>
                <div className="eyebrow"><span>Cost</span></div>
                <div style={{ fontSize: "1.5rem", fontWeight: 600 }}>${u!.totals.cost_actual_usd.toFixed(4)}</div>
              </SoftCard>
            )}
            {u!.totals.savings_usd != null && (
              <SoftCard style={{ minWidth: 120, textAlign: "center" }}>
                <div className="eyebrow"><span>Savings</span></div>
                <div style={{ fontSize: "1.5rem", fontWeight: 600 }}>${u!.totals.savings_usd.toFixed(4)}</div>
              </SoftCard>
            )}
          </div>

          {u!.top_models.length > 0 && (
            <>
              <div className="eyebrow" style={{ marginBottom: 8 }}><span>Top models</span></div>
              <SoftCard>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead>
                    <tr>
                      <th style={{ textAlign: "left", paddingBottom: 6, fontSize: "0.75rem", opacity: 0.6 }}>Model</th>
                      <th style={{ textAlign: "right", paddingBottom: 6, fontSize: "0.75rem", opacity: 0.6 }}>Calls</th>
                    </tr>
                  </thead>
                  <tbody>
                    {u!.top_models.map((row) => (
                      <tr key={row.routed_model}>
                        <td className="mono" style={{ paddingBottom: 4, fontSize: "0.85rem" }}>{row.routed_model}</td>
                        <td style={{ textAlign: "right", paddingBottom: 4, fontSize: "0.85rem" }}>{row.calls}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </SoftCard>
            </>
          )}
        </>
      )}
    </div>
  );
}

function OperatorSettings() {
  const [theme, setTheme] = useState(localStorage.getItem("brains.theme") ?? "dark");
  return (
    <div>
      <div className="eyebrow"><span>Operator</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>Preferences</h2>
      <div className="row">
        <span className="meta">Theme</span>
        <button
          className="btn"
          onClick={() => {
            const next = theme === "dark" ? "light" : "dark";
            setTheme(next);
            document.documentElement.dataset.theme = next;
            localStorage.setItem("brains.theme", next);
          }}
        >
          {theme}
        </button>
      </div>
    </div>
  );
}

function About() {
  return (
    <SoftCard>
      <div className="eyebrow"><span>About</span></div>
      <h2 style={{ margin: "8px 0" }}>Brains operator console</h2>
      <p className="meta">
        Brains web console served from the FastAPI wheel under /app, with
        realtime updates over /v1/ws.
      </p>
    </SoftCard>
  );
}
