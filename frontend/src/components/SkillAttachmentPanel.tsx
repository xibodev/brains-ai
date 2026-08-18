import { useState } from "react";
import { api, formatApiError } from "../api/client";
import type { Skill, SkillAttachment } from "../api/types";
import { useAsync, type AsyncState } from "../store/useAsync";
import { useToast } from "./Toast";
import { Select } from "./Field";
import { SoftCard } from "./SoftCard";
import { Loading } from "./EmptyState";

// Shared attach/detach control for Skills attached to a Persona or Project
// (BL-P1-08/F10). Attached Skills enter that entity's spawned Session context
// with provenance (`brains.control.skills.resolve_context_for_session`) —
// this panel is the one place either surface offers to change that set.
export function SkillAttachmentPanel({
  orgSlug,
  attached,
  onAttach,
  onDetach,
}: {
  orgSlug: string | undefined;
  attached: AsyncState<SkillAttachment[]>;
  onAttach: (skillId: string | number) => Promise<unknown>;
  onDetach: (skillId: string | number) => Promise<unknown>;
}) {
  const { toast } = useToast();
  const orgSkills = useAsync<Skill[]>(
    () => (orgSlug ? api.listSkills(orgSlug).catch(() => []) : Promise.resolve([])),
    [orgSlug],
  );
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);

  const attachedIds = new Set((attached.data ?? []).map((a) => String(a.skill_id)));
  const options = (orgSkills.data ?? []).filter((s) => !attachedIds.has(String(s.id)));

  const attach = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await onAttach(selected);
      setSelected("");
      attached.refetch();
      toast("Skill attached");
    } catch (e) {
      toast(formatApiError("Attach skill", e));
    } finally {
      setBusy(false);
    }
  };

  const detach = async (skillId: string | number) => {
    try {
      await onDetach(skillId);
      attached.refetch();
      toast("Skill detached");
    } catch (e) {
      toast(formatApiError("Detach skill", e));
    }
  };

  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 8 }}><span>Attached skills</span></div>
      {attached.loading && attached.data === undefined ? (
        <Loading />
      ) : (attached.data ?? []).length === 0 ? (
        <div className="meta" style={{ marginBottom: 12 }}>No Skills attached yet.</div>
      ) : (
        <div className="card-list" style={{ marginBottom: 12 }}>
          {(attached.data ?? []).map((a) => (
            <SoftCard key={String(a.skill_id)}>
              <div className="row spread">
                <span>{a.name}</span>
                <div className="row" style={{ gap: 8 }}>
                  <span className="meta mono">{a.slug}</span>
                  <button
                    className="btn small"
                    onClick={() => void detach(a.skill_id)}
                  >
                    Detach
                  </button>
                </div>
              </div>
            </SoftCard>
          ))}
        </div>
      )}
      {options.length > 0 && (
        <div className="row" style={{ gap: 8, alignItems: "flex-end" }}>
          <div style={{ flex: 1 }}>
            <Select
              label="Attach a skill"
              value={selected}
              onChange={setSelected}
              options={[
                { value: "", label: "-- select skill --" },
                ...options.map((s) => ({ value: String(s.id), label: s.name })),
              ]}
            />
          </div>
          <button className="btn" disabled={busy || !selected} onClick={() => void attach()}>
            Attach
          </button>
        </div>
      )}
    </div>
  );
}
