// React hooks over the multiplexed realtime client.

import { useEffect, useRef, useState } from "react";
import { realtime, type ConnState, type Envelope } from "./client";

// Subscribe to one (or several) topics; invokes `onEvent` for each frame.
// The callback is held in a ref so changing it doesn't churn subscriptions.
export function useTopic(
  topics: string | string[] | null | undefined,
  onEvent: (env: Envelope) => void,
): void {
  const cb = useRef(onEvent);
  cb.current = onEvent;

  const key = Array.isArray(topics) ? topics.join("|") : topics ?? "";

  useEffect(() => {
    if (!key) return;
    const list = key.split("|").filter(Boolean);
    const unsubs = list.map((t) =>
      realtime.subscribe(t, (env) => cb.current(env)),
    );
    return () => unsubs.forEach((u) => u());
  }, [key]);
}

// Track the socket connection state for a connection banner.
export function useConnState(): ConnState {
  const [state, setState] = useState<ConnState>(realtime.connState);
  useEffect(() => realtime.onConn(setState), []);
  return state;
}
