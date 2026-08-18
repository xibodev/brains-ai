// Dock context — shares the chat dock's selected session + collapse state and
// the bell/inbox count across the shell, so "Open in chat" deep-links work and
// the topbar bell + Inbox badge derive from one number.

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

interface DockContextValue {
  selectedSession: string | number | null;
  openInChat: (sessionId: string | number) => void;
  collapsed: boolean;
  toggleCollapsed: () => void;
  inboxCount: number;
  setInboxCount: (n: number) => void;
}

const DockContext = createContext<DockContextValue | null>(null);

export function DockProvider({ children }: { children: ReactNode }) {
  const [selectedSession, setSelected] = useState<string | number | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [inboxCount, setInboxCount] = useState(0);

  const openInChat = useCallback((sessionId: string | number) => {
    setSelected(sessionId);
    setCollapsed(false);
  }, []);

  const value = useMemo<DockContextValue>(
    () => ({
      selectedSession,
      openInChat,
      collapsed,
      toggleCollapsed: () => setCollapsed((c) => !c),
      inboxCount,
      setInboxCount,
    }),
    [selectedSession, openInChat, collapsed, inboxCount],
  );

  return <DockContext.Provider value={value}>{children}</DockContext.Provider>;
}

export function useDock(): DockContextValue {
  const ctx = useContext(DockContext);
  if (!ctx) throw new Error("useDock must be used within DockProvider");
  return ctx;
}
