// Lenovmail — authored by satuapps (satuapps.com)
// Data hooks for the mail page. No react-query (not available in this project):
// manual fetch + local state, sufficient for MailPage's three-panel needs.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import type { Account, Attachment, Folder, Message, MessageQuery } from "../../types";

/** Value that becomes stable `delay` ms after the last change. */
export function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

function sleep(ms: number): Promise<void> {
  // Promise.withResolvers needs lib ES2024+; tsconfig.json is locked to ES2023.
  return new Promise((resolve) => setTimeout(resolve, ms));
}

interface AccountsState {
  accounts: Account[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function useAccounts(): AccountsState {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listAccounts()
      .then((list) => {
        if (!cancelled) setAccounts(list);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Failed to load accounts");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tick]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { accounts, loading, error, reload };
}

interface FoldersState {
  folders: Folder[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function useFolders(accountId: string | null): FoldersState {
  const [folders, setFolders] = useState<Folder[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (accountId === null) {
      setFolders([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listFolders(accountId)
      .then((list) => {
        if (!cancelled) setFolders(list);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Failed to load folders");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [accountId, tick]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { folders, loading, error, reload };
}

export interface MessagesState {
  items: Message[];
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => void;
  reload: () => void;
  /** Apply a local flag patch (used after mark/delete actions so the UI stays responsive). */
  patchItem: (id: string, patch: Partial<Message>) => void;
  removeItem: (id: string) => void;
}

/** Keyset-paginated message list for one account + filters. Refetches from the start when filters change. */
export function useMessages(accountId: string | null, query: MessageQuery): MessagesState {
  const [items, setItems] = useState<Message[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);
  const requestId = useRef(0);
  const queryKey = JSON.stringify(query);

  useEffect(() => {
    if (accountId === null) {
      setItems([]);
      setCursor(null);
      return;
    }
    const myRequest = ++requestId.current;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listMessages(accountId, { ...query, limit: 50, cursor: undefined })
      .then((page) => {
        if (cancelled || requestId.current !== myRequest) return;
        setItems(page.items);
        setCursor(page.next_cursor);
      })
      .catch((err: unknown) => {
        if (cancelled || requestId.current !== myRequest) return;
        setError(err instanceof ApiError ? err.message : "Failed to load messages");
      })
      .finally(() => {
        if (cancelled || requestId.current !== myRequest) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, queryKey, reloadTick]);

  const loadMore = useCallback(() => {
    if (accountId === null || cursor === null || loadingMore) return;
    const myRequest = requestId.current;
    setLoadingMore(true);
    api
      .listMessages(accountId, { ...query, limit: 50, cursor })
      .then((page) => {
        if (requestId.current !== myRequest) return;
        setItems((prev) => {
          const seen = new Set(prev.map((item) => item.id));
          return [...prev, ...page.items.filter((item) => !seen.has(item.id))];
        });
        setCursor(page.next_cursor);
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : "Failed to load more messages");
      })
      .finally(() => setLoadingMore(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, cursor, loadingMore, queryKey]);

  const reload = useCallback(() => setReloadTick((n) => n + 1), []);

  const patchItem = useCallback((id: string, patch: Partial<Message>) => {
    setItems((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  }, []);

  const removeItem = useCallback((id: string) => {
    setItems((prev) => prev.filter((item) => item.id !== id));
  }, []);

  return {
    items,
    loading,
    loadingMore,
    error,
    hasMore: cursor !== null,
    loadMore,
    reload,
    patchItem,
    removeItem,
  };
}

interface MessageDetailState {
  message: Message | null;
  bodyText: string | null;
  bodyHtml: string | null;
  bodyState: string | null;
  attachments: Attachment[];
  loading: boolean;
  error: string | null;
  polling: boolean;
  startPolling: () => void;
  stopPolling: () => void;
  reload: () => void;
}

/** Single message detail + body, with manual polling (the "Load content" button) until `body_state === 'full'`. */
export function useMessageDetail(messageId: string | null): MessageDetailState {
  const [message, setMessage] = useState<Message | null>(null);
  const [bodyText, setBodyText] = useState<string | null>(null);
  const [bodyHtml, setBodyHtml] = useState<string | null>(null);
  const [bodyState, setBodyState] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);
  const [tick, setTick] = useState(0);
  const stopRef = useRef(false);

  const fetchOnce = useCallback(async (id: string) => {
    const [msg, body] = await Promise.all([api.getMessage(id), api.getBody(id)]);
    setMessage(msg);
    setBodyText(body.text);
    setBodyHtml(body.html);
    setBodyState(body.body_state);
    setAttachments(body.attachments);
    return body.body_state;
  }, []);

  useEffect(() => {
    if (messageId === null) {
      setMessage(null);
      setBodyText(null);
      setBodyHtml(null);
      setBodyState(null);
      setAttachments([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchOnce(messageId)
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Failed to load message");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [messageId, fetchOnce, tick]);

  const stopPolling = useCallback(() => {
    stopRef.current = true;
    setPolling(false);
  }, []);

  const startPolling = useCallback(() => {
    if (messageId === null || polling) return;
    stopRef.current = false;
    setPolling(true);
    const id = messageId;
    void (async () => {
      while (!stopRef.current) {
        try {
          const state = await fetchOnce(id);
          if (state === "full" || stopRef.current) break;
        } catch (err) {
          setError(err instanceof ApiError ? err.message : "Failed to load message content");
          break;
        }
        await sleep(2000);
      }
      setPolling(false);
    })();
  }, [messageId, polling, fetchOnce]);

  const reload = useCallback(() => setTick((n) => n + 1), []);

  return {
    message,
    bodyText,
    bodyHtml,
    bodyState,
    attachments,
    loading,
    error,
    polling,
    startPolling,
    stopPolling,
    reload,
  };
}
