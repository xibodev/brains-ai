import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { coreRoute } from "../coreRoutes";
import { useOperator } from "../store/OperatorContext";

interface Cmd {
  label: string;
  hint?: string;
  to: string;
}

const NAV_COMMANDS: Cmd[] = [
  { label: "Command Center", to: "/command-center", hint: "view" },
  { label: "Workspaces", to: "/workspaces", hint: "view" },
  { label: "Coordination", to: "/coordination", hint: "view" },
  { label: "Governance", to: "/governance", hint: "view" },
  { label: "Operations", to: "/operations", hint: "view" },
];

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const navigate = useNavigate();
  const { catalog } = useOperator();

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

  const commands = [
    ...NAV_COMMANDS,
    ...(catalog?.data ?? []).map((capability) => ({
      label: capability.label,
      to: `/act?capability=${encodeURIComponent(capability.key)}`,
      hint: capability.enabled ? capability.scope : capability.transport.replace("_", " "),
    })),
  ];
  const needle = query.trim().toLowerCase();
  const results = needle
    ? commands.filter((command) => command.label.toLowerCase().includes(needle))
    : commands;

  if (!open) return null;

  const go = (to: string) => {
    navigate(coreRoute(to));
    setOpen(false);
  };

  return (
    <div className="palette-scrim" onClick={() => setOpen(false)}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <input
          autoFocus
          placeholder="Find a view or typed action"
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
