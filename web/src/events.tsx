// Lenovmail — authored by satuapps
import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ServerEvent } from "./types";

export type StreamStatus = "connecting" | "open" | "closed";
type Handler = (event: ServerEvent) => void;

interface StreamValue {
  status: StreamStatus;
  /** Register a handler; returns a function to unsubscribe it. */
  subscribe: (handler: Handler) => () => void;
}

const StreamContext = createContext<StreamValue | null>(null);

/**
 * One SSE connection per tab, shared by every page through context. Opening
 * an `EventSource` per page would exhaust connections to the API and duplicate
 * messages; `EventSource` itself already reconnects automatically.
 */
export function EventsProvider({ children }: { children: ReactNode }) {
  const handlers = useRef(new Set<Handler>());
  const [status, setStatus] = useState<StreamStatus>("connecting");

  useEffect(() => {
    const source = new EventSource("/api/events", { withCredentials: true });
    source.onopen = () => setStatus("open");
    source.onerror = () => setStatus("closed");
    source.onmessage = (event: MessageEvent<string>) => {
      if (!event.data) return;
      let parsed: ServerEvent;
      try {
        parsed = JSON.parse(event.data) as ServerEvent;
      } catch {
        return;
      }
      for (const handler of handlers.current) handler(parsed);
    };
    return () => {
      source.close();
      handlers.current.clear();
      setStatus("closed");
    };
  }, []);

  const value = useMemo<StreamValue>(
    () => ({
      status,
      subscribe: (handler) => {
        handlers.current.add(handler);
        return () => handlers.current.delete(handler);
      },
    }),
    [status],
  );

  return <StreamContext.Provider value={value}>{children}</StreamContext.Provider>;
}

function useStream(): StreamValue {
  const value = useContext(StreamContext);
  if (value === null) throw new Error("Event stream is only available inside <EventsProvider>");
  return value;
}

/** Subscribe to events for the component's lifetime. The latest handler is used without resubscribing. */
export function useEventStream(handler: Handler): StreamStatus {
  const { status, subscribe } = useStream();
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  useEffect(() => subscribe((event) => handlerRef.current(event)), [subscribe]);
  return status;
}

export function useStreamStatus(): StreamStatus {
  return useStream().status;
}
