// Onboarding progress as eyebrow steps (WS4 §5).
export function Stepper({ total, current }: { total: number; current: number }) {
  return (
    <div>
      <div className="eyebrow">
        <span>
          Step {current} / {total}
        </span>
      </div>
      <div className="stepper">
        {Array.from({ length: total }).map((_, i) => (
          <div key={i} className={`step ${i < current ? "done" : ""}`} />
        ))}
      </div>
    </div>
  );
}
