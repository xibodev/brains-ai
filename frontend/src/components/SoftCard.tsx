import type { CSSProperties, ReactNode } from "react";

// Near-borderless card; `cascade` offsets it to read as overlapping paper.
export function SoftCard({
  children,
  cascade,
  interactive,
  onClick,
  style,
  accent,
}: {
  children: ReactNode;
  cascade?: boolean;
  interactive?: boolean;
  onClick?: () => void;
  style?: CSSProperties;
  accent?: string | null;
}) {
  const cls = ["softcard"];
  if (cascade) cls.push("cascade");
  if (interactive || onClick) cls.push("interactive");
  const merged: CSSProperties = accent
    ? { ...style, ["--persona-accent" as string]: accent }
    : style ?? {};
  return (
    <div
      className={cls.join(" ")}
      onClick={onClick}
      style={merged}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
    >
      {children}
    </div>
  );
}
