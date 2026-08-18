import { useEffect, useRef, useState } from "react";
import { useOrg } from "../store/OrgContext";
import { api } from "../api/client";

function toSlug(name: string): string {
  return name.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "");
}

export function OrgSwitcher() {
  const { orgs, activeOrg, setActiveOrg, loading, refresh } = useOrg();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [saving, setSaving] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setCreating(false);
        setNewName("");
        setCreateError(null);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const label = loading && !activeOrg ? "..." : (activeOrg?.name ?? "No org");

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    const slug = toSlug(name);
    if (!slug) return;
    setSaving(true);
    setCreateError(null);
    try {
      await api.createOrg({ slug, name });
      refresh();
      setActiveOrg(slug);
      setCreating(false);
      setNewName("");
      setOpen(false);
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : "Create failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="dropdown" ref={ref}>
      <button onClick={() => setOpen((o) => !o)}>
        &#9662; <span data-testid="active-org">{label}</span>
      </button>
      {open && (
        <div className="dropdown-menu">
          {orgs.length === 0 && (
            <div className="meta" style={{ padding: 8 }}>
              No orgs yet
            </div>
          )}
          {orgs.map((o) => (
            <button
              key={o.slug}
              className={o.slug === activeOrg?.slug ? "active" : ""}
              onClick={() => {
                setActiveOrg(o.slug);
                setOpen(false);
              }}
            >
              {o.name}
            </button>
          ))}
          <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)", marginTop: 4, paddingTop: 4 }}>
            {!creating ? (
              <button
                style={{ opacity: 0.7, fontSize: "0.8rem" }}
                onClick={() => setCreating(true)}
              >
                + New org
              </button>
            ) : (
              <div style={{ padding: "6px 8px", display: "flex", flexDirection: "column", gap: 6 }}>
                <input
                  autoFocus
                  type="text"
                  placeholder="Org name"
                  value={newName}
                  onChange={(e) => { setNewName(e.target.value); setCreateError(null); }}
                  onKeyDown={(e) => { if (e.key === "Enter") void handleCreate(); if (e.key === "Escape") { setCreating(false); setNewName(""); } }}
                  style={{ fontSize: "0.85rem", padding: "4px 6px" }}
                />
                {newName.trim() && (
                  <div className="meta" style={{ fontSize: "0.75rem" }}>
                    slug: {toSlug(newName.trim())}
                  </div>
                )}
                {createError && (
                  <div className="meta" style={{ color: "var(--red, #f66)", fontSize: "0.75rem" }}>
                    {createError}
                  </div>
                )}
                <div style={{ display: "flex", gap: 6 }}>
                  <button
                    className="btn primary"
                    style={{ flex: 1, fontSize: "0.8rem" }}
                    disabled={saving || !newName.trim()}
                    onClick={() => void handleCreate()}
                  >
                    {saving ? "..." : "Create"}
                  </button>
                  <button
                    className="btn"
                    style={{ fontSize: "0.8rem" }}
                    onClick={() => { setCreating(false); setNewName(""); setCreateError(null); }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
