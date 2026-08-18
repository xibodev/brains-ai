import type { ReactNode } from "react";
import { EyebrowLabel } from "../components/EyebrowLabel";

// Editorial screen header: small-caps eyebrow over a serif H1, optional actions.
export function ScreenHead({
  eyebrow,
  title,
  actions,
}: {
  eyebrow: string;
  title: string;
  actions?: ReactNode;
}) {
  return (
    <div className="main-head">
      <div className="row spread">
        <EyebrowLabel>{eyebrow}</EyebrowLabel>
        {actions}
      </div>
      <h1>{title}</h1>
    </div>
  );
}
