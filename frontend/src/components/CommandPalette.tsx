import { useCallback, useEffect, useState } from "react";
import { useCoreNavigation } from "../coreRoutes";
import { useOperator } from "../store/OperatorContext";
import { useDialogFocus } from "./useDialogFocus";

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
  const navigation = useCoreNavigation();
  const { catalog } = useOperator();
  const close = useCallback(() => setOpen(false), []);
  const dialogRef = useDialogFocus<HTMLDivElement>(open, close);

  useEffect(() => {
    const reset = () => {
      setQuery("");
      setActive(0);
    };
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
        reset();
      }
    };
    const onOpen = () => {
      setOpen(true);
      reset();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("brains:open-command-palette", onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("brains:open-command-palette", onOpen);
    };
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
    navigation.open(to);
    setOpen(false);
  };

  return (
    <div className="palette-scrim" onClick={close}>
      <div ref={dialogRef} className="palette" role="dialog" aria-modal="true" aria-label="Command palette" tabIndex={-1} onClick={(e) => e.stopPropagation()}>
        <input
          data-initial-focus
          role="combobox"
          aria-label="Find a view or typed action"
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-expanded="true"
          aria-controls="command-palette-results"
          aria-activedescendant={results[active] ? `command-palette-option-${active}` : undefined}
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
        <div id="command-palette-results" className="results" role="listbox" aria-label="Available commands">
          {results.map((c, i) => (
            <button
              key={c.to}
              id={`command-palette-option-${i}`}
              className={i === active ? "active" : ""}
              role="option"
              tabIndex={-1}
              aria-selected={i === active}
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
