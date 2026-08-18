import type { ReactNode } from "react";

// Small-caps section label — the "border replacement" (WS4 §0/§6). Wraps its
// text in a span so the sidebar mini-mode can hide the text but keep an icon.
export function EyebrowLabel({ children }: { children: ReactNode }) {
  return (
    <div className="eyebrow">
      <span>{children}</span>
    </div>
  );
}
