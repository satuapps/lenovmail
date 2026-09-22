// Lenovmail — authored by satuapps (satuapps.com)
// MailPage center column: search box + filters + virtualized message list.
import { useEffect, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Message } from "../../types";
import { senderLabel, shortDate } from "../../format";

const ROW_HEIGHT = 64;

interface MessageListPaneProps {
  items: Message[];
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  hasMore: boolean;
  onLoadMore: () => void;
  activeMessageId: string | null;
  onSelectMessage: (id: string) => void;
  searchInput: string;
  onSearchInputChange: (value: string) => void;
  unreadOnly: boolean;
  onToggleUnread: () => void;
  flaggedOnly: boolean;
  onToggleFlagged: () => void;
  attachmentsOnly: boolean;
  onToggleAttachments: () => void;
  updateBanner: string | null;
  onDismissUpdateBanner: () => void;
  onRefresh: () => void;
}

export default function MessageListPane({
  items,
  loading,
  loadingMore,
  error,
  hasMore,
  onLoadMore,
  activeMessageId,
  onSelectMessage,
  searchInput,
  onSearchInputChange,
  unreadOnly,
  onToggleUnread,
  flaggedOnly,
  onToggleFlagged,
  attachmentsOnly,
  onToggleAttachments,
  updateBanner,
  onDismissUpdateBanner,
  onRefresh,
}: MessageListPaneProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const loadMoreRef = useRef(onLoadMore);
  loadMoreRef.current = onLoadMore;

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 8,
  });

  const rangeEndIndex = virtualizer.range?.endIndex ?? null;
  useEffect(() => {
    if (rangeEndIndex === null) return;
    if (rangeEndIndex >= items.length - 5 && hasMore && !loadingMore) {
      loadMoreRef.current();
    }
  }, [rangeEndIndex, items.length, hasMore, loadingMore]);

  return (
    <section className="flex h-full min-h-0 w-96 shrink-0 flex-col border-r border-ink-600 bg-ink-900">
      <div className="space-y-2 border-b border-ink-600 p-2">
        <input
          type="search"
          className="input"
          placeholder="Search mail…"
          value={searchInput}
          onChange={(event) => onSearchInputChange(event.target.value)}
        />
        <div className="flex flex-wrap gap-1.5">
          <button
            type="button"
            className={`chip ${unreadOnly ? "chip-active" : ""}`}
            onClick={onToggleUnread}
          >
            Unread
          </button>
          <button
            type="button"
            className={`chip ${flaggedOnly ? "chip-active" : ""}`}
            onClick={onToggleFlagged}
          >
            Flagged
          </button>
          <button
            type="button"
            className={`chip ${attachmentsOnly ? "chip-active" : ""}`}
            onClick={onToggleAttachments}
          >
            Has attachments
          </button>
        </div>
      </div>

      {updateBanner !== null && (
        <div className="flex items-center justify-between gap-2 border-b border-accent-strong/40 bg-accent-strong/10 px-3 py-1.5 text-xs text-accent">
          <span>{updateBanner}</span>
          <div className="flex items-center gap-2">
            <button type="button" className="underline" onClick={onRefresh}>
              Refresh
            </button>
            <button type="button" className="text-fg-dim" onClick={onDismissUpdateBanner}>
              ×
            </button>
          </div>
        </div>
      )}

      {error !== null && <p className="border-b border-ink-600 px-3 py-2 text-xs text-danger">{error}</p>}

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        {loading && items.length === 0 && (
          <p className="p-4 text-center text-sm text-fg-muted">Loading messages…</p>
        )}
        {!loading && items.length === 0 && (
          <p className="p-4 text-center text-sm text-fg-muted">No messages.</p>
        )}
        {items.length > 0 && (
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const message = items[virtualRow.index];
              if (message === undefined) return null;
              const isActive = message.id === activeMessageId;
              return (
                <button
                  key={message.id}
                  type="button"
                  onClick={() => onSelectMessage(message.id)}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    height: virtualRow.size,
                    transform: `translateY(${virtualRow.start}px)`,
                  }}
                  className={`flex flex-col justify-center gap-0.5 border-b border-white/5 px-3 py-1 text-left transition-colors ${
                    isActive ? "row-active" : "hover:bg-white/[0.04]"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className={`flex min-w-0 items-center gap-1.5 truncate text-sm leading-tight ${
                        message.seen ? "text-fg-muted" : "font-semibold text-fg"
                      }`}
                    >
                      {!message.seen && <span className="led led-accent" aria-hidden />}
                      <span className="truncate">{senderLabel(message.from_name, message.from_addr)}</span>
                    </span>
                    <span className="mono shrink-0 text-[11px] text-fg-dim">{shortDate(message.internal_date)}</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span
                      className={`truncate text-sm leading-tight ${message.seen ? "text-fg-muted" : "text-fg"}`}
                    >
                      {message.subject || "(no subject)"}
                    </span>
                    {message.flagged && (
                      <span className="shrink-0 text-warn" aria-label="flagged">
                        {"\u2691"}
                      </span>
                    )}
                    {message.has_attachments && (
                      <span className="shrink-0 text-fg-dim" aria-label="has attachments">
                        {"\u{1F4CE}"}
                      </span>
                    )}
                  </div>
                  <span className="truncate text-xs leading-tight text-fg-dim">{message.snippet}</span>
                </button>
              );
            })}
          </div>
        )}
        {loadingMore && <p className="p-2 text-center text-xs text-fg-dim">Loading more…</p>}
      </div>
    </section>
  );
}
