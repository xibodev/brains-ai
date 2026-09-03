import { useCallback, type ReactNode } from "react";
import { useDialogFocus } from "./useDialogFocus";

// Slide-over panel (the issue drawer slides over, never navigates away).
export function Drawer({
  open,
  onClose,
  children,
}: {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const close = useCallback(() => onClose(), [onClose]);
  const dialogRef = useDialogFocus<HTMLDivElement>(open, close);
  if (!open) return null;
  return (
    <>
      <div className="drawer-scrim" onClick={close} aria-hidden="true" />
      <div ref={dialogRef} className="drawer" role="dialog" aria-modal="true" aria-label="Details" tabIndex={-1}>
        <div className="row spread" style={{ marginBottom: 16 }}>
          <span />
          <button className="icon-btn" onClick={close} aria-label="Close dialog" data-initial-focus>
            ✕
          </button>
        </div>
        {children}
      </div>
    </>
  );
}
