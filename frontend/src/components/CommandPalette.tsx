import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

interface Cmd {
  label: string;
  hint?: string;
  to: string;
}

const NAV_COMMANDS: Cmd[] = [
  { label: "Inbox / Approvals", to: "/inbox", hint: "workspace" },
  { label: "Sessions", to: "/sessions", hint: "workspace" },
  { label: "Personas", to: "/personas", hint: "workspace" },
  { label: "Pods", to: "/pods", hint: "workspace" },
  { label: "Projects", to: "/projects", hint: "workspace" },
  { label: "Issues board", to: "/issues", hint: "workspace" },
  { label: "Runtimes", to: "/runtimes", hint: "configure" },
  { label: "Config · Providers", to: "/config/providers", hint: "configure" },
  { label: "Settings · Org", to: "/settings/org", hint: "configure" },
];

// ⌘K fuzzy nav across entities (WS4 §7). Nav-only for the static shell; entity
// search slots in here once a search endpoint lands.
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const navigate = useNavigate();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
        setQuery("");
        setActive(0);
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return NAV_COMMANDS;
    return NAV_COMMANDS.filter((c) => c.label.toLowerCase().includes(q));
  }, [query]);

  if (!open) return null;

  const go = (to: string) => {
    navigate(to);
    setOpen(false);
  };

  return (
    <div className="palette-scrim" onClick={() => setOpen(false)}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <input
          autoFocus
          placeholder="Jump to…"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") setActive((a) => Math.min(a + 1, results.length - 1));
            else if (e.key === "ArrowUp") setActive((a) => Math.max(a - 1, 0));
            else if (e.key === "Enter" && results[active]) go(results[active].to);
          }}
        />
        <div className="results">
          {results.map((c, i) => (
            <button
              key={c.to}
              className={i === active ? "active" : ""}
              onMouseEnter={() => setActive(i)}
              onClick={() => go(c.to)}
            >
              <span>{c.label}</span>
              {c.hint && (
                <span className="meta" style={{ marginLeft: "auto" }}>
                  {c.hint}
                </span>
              )}
            </button>
          ))}
          {results.length === 0 && (
            <div className="meta" style={{ padding: 12 }}>
              No matches
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
