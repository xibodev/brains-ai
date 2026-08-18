import type { ReactNode } from "react";

export interface RailItem {
  key: string;
  label: string;
  section?: string; // optional grouping heading
}

// Master-detail with a tab-like rail using the same subtle-fill active state
// as sidebar nav. `railOnLeft` (the founder's want) renders the rail as a
// continuous left edge with hairline-only separation — no boxed gutter.
export function MasterDetail({
  items,
  activeKey,
  onSelect,
  railHeader,
  railFooter,
  children,
  railOnLeft,
}: {
  items: RailItem[];
  activeKey: string | null;
  onSelect: (key: string) => void;
  railHeader?: ReactNode;
  railFooter?: ReactNode;
  children: ReactNode;
  railOnLeft?: boolean;
}) {
  // group items by section preserving order
  const groups: { section?: string; items: RailItem[] }[] = [];
  for (const it of items) {
    const last = groups[groups.length - 1];
    if (last && last.section === it.section) last.items.push(it);
    else groups.push({ section: it.section, items: [it] });
  }

  return (
    <div className={`masterdetail ${railOnLeft ? "rail-left" : ""}`}>
      <div className="md-rail">
        {railHeader}
        {groups.map((g, gi) => (
          <div key={gi} className={gi > 0 ? "rail-section" : undefined}>
            {g.section && (
              <div className="eyebrow" style={{ padding: "8px 12px 4px" }}>
                <span>{g.section}</span>
              </div>
            )}
            {g.items.map((it) => (
              <button
                key={it.key}
                className={`rail-item ${activeKey === it.key ? "active" : ""}`}
                onClick={() => onSelect(it.key)}
              >
                {it.label}
              </button>
            ))}
          </div>
        ))}
        {railFooter}
      </div>
      <div className="md-detail">{children}</div>
    </div>
  );
}
