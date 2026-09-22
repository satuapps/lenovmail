// Lenovmail — authored by satuapps (satuapps.com)
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../api";
import { relativeTime } from "../../format";
import { useDebouncedValue } from "../mail/mailHooks";
import type { AgentAuditEntry, SessionInfo } from "../../types";

const OUTCOME_CHIP: Record<string, string> = {
  ok: "chip chip-ok",
  denied: "chip chip-warn",
  error: "chip chip-danger",
};

const AUDIT_PAGE = 100;

/** Browser sessions the user can revoke, plus the audit trail of agent and reveal actions. */
export default function SecurityPage() {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  const [audit, setAudit] = useState<AgentAuditEntry[] | null>(null);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [toolFilter, setToolFilter] = useState("");
  const [outcomeFilter, setOutcomeFilter] = useState("");
  const [loadingMore, setLoadingMore] = useState(false);
  const [exhausted, setExhausted] = useState(false);
  const debouncedTool = useDebouncedValue(toolFilter, 300);

  const loadSessions = useCallback(async () => {
    setSessionError(null);
    try {
      setSessions(await api.listSessions());
    } catch (err) {
      setSessionError(
        err instanceof ApiError ? err.message : "Failed to load sessions.",
      );
    }
  }, []);

  const loadAudit = useCallback(async () => {
    setAuditError(null);
    try {
      const rows = await api.listAudit({
        limit: AUDIT_PAGE,
        tool: debouncedTool || undefined,
        outcome: outcomeFilter || undefined,
      });
      setAudit(rows);
      setExhausted(rows.length < AUDIT_PAGE);
    } catch (err) {
      setAuditError(
        err instanceof ApiError ? err.message : "Failed to load audit log.",
      );
    }
  }, [debouncedTool, outcomeFilter]);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    void loadAudit();
  }, [loadAudit]);

  async function loadMore() {
    const last = audit?.at(-1);
    if (!last) return;
    setLoadingMore(true);
    try {
      const rows = await api.listAudit({
        limit: AUDIT_PAGE,
        tool: debouncedTool || undefined,
        outcome: outcomeFilter || undefined,
        before_id: last.id,
      });
      setAudit((prev) => [...(prev ?? []), ...rows]);
      setExhausted(rows.length < AUDIT_PAGE);
    } catch (err) {
      setAuditError(
        err instanceof ApiError ? err.message : "Failed to load audit log.",
      );
    } finally {
      setLoadingMore(false);
    }
  }

  async function revoke(session: SessionInfo) {
    if (
      session.current &&
      !window.confirm("Revoke this session? You will be signed out.")
    )
      return;
    setRevokingId(session.id);
    try {
      await api.revokeSession(session.id);
      if (session.current) {
        navigate("/login", { replace: true });
        return;
      }
      await loadSessions();
    } catch (err) {
      setSessionError(
        err instanceof ApiError ? err.message : "Failed to revoke session.",
      );
    } finally {
      setRevokingId(null);
    }
  }

  return (
    <div className="mx-auto max-w-4xl p-6">
      <h1 className="page-title mb-4">Security</h1>

      <section className="panel mb-4 rounded-xl p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="page-title">Browser sessions</h2>
          <button
            type="button"
            className="btn"
            onClick={() => void loadSessions()}
          >
            Refresh
          </button>
        </div>

        {sessionError && (
          <div className="mb-3 chip chip-danger">{sessionError}</div>
        )}
        {sessions === null && !sessionError && (
          <div className="text-sm text-fg-muted">Loading…</div>
        )}
        {sessions !== null && sessions.length === 0 && (
          <div className="text-sm text-fg-muted">No sessions recorded yet.</div>
        )}

        <div className="flex flex-col gap-2">
          {sessions?.map((session) => (
            <div
              key={session.id}
              className="flex items-center justify-between gap-3 border-t border-ink-600 pt-2"
            >
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <span
                    className="truncate text-sm text-fg"
                    title={session.user_agent ?? undefined}
                  >
                    {session.user_agent ?? "unknown device"}
                  </span>
                  {session.current && (
                    <span className="chip chip-ok shrink-0">this device</span>
                  )}
                </div>
                <div className="mono text-xs text-fg-dim">
                  {session.ip ?? "—"} · started{" "}
                  {relativeTime(session.created_at)} · last seen{" "}
                  {relativeTime(session.last_seen_at)}
                </div>
              </div>
              <button
                type="button"
                className="btn btn-danger shrink-0"
                disabled={revokingId === session.id}
                onClick={() => void revoke(session)}
              >
                {revokingId === session.id ? "Revoking…" : "Revoke"}
              </button>
            </div>
          ))}
        </div>
      </section>

      <section className="panel rounded-xl p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h2 className="page-title mr-auto">Agent audit</h2>
          <input
            className="input w-48"
            placeholder="Filter by tool…"
            value={toolFilter}
            onChange={(event) => setToolFilter(event.target.value)}
          />
          <select
            className="input w-32"
            value={outcomeFilter}
            onChange={(event) => setOutcomeFilter(event.target.value)}
          >
            <option value="">all outcomes</option>
            <option value="ok">ok</option>
            <option value="denied">denied</option>
            <option value="error">error</option>
          </select>
        </div>

        {auditError && (
          <div className="mb-3 chip chip-danger">{auditError}</div>
        )}
        {audit === null && !auditError && (
          <div className="text-sm text-fg-muted">Loading…</div>
        )}
        {audit !== null && audit.length === 0 && (
          <div className="text-sm text-fg-muted">
            No audit entries match these filters.
          </div>
        )}

        {audit !== null && audit.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-[11px] uppercase tracking-[0.14em] text-fg-dim">
                <tr>
                  <th className="px-3 py-2 font-medium">When</th>
                  <th className="px-3 py-2 font-medium">Tool</th>
                  <th className="px-3 py-2 font-medium">Outcome</th>
                  <th className="px-3 py-2 font-medium">Targets</th>
                  <th className="px-3 py-2 font-medium">Error</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((row) => (
                  <tr
                    key={row.id}
                    className="border-t border-ink-600 align-top"
                  >
                    <td className="px-3 py-2 whitespace-nowrap text-fg-muted">
                      {relativeTime(row.created_at)}
                    </td>
                    <td className="mono px-3 py-2 text-fg">{row.tool}</td>
                    <td className="px-3 py-2">
                      <span className={OUTCOME_CHIP[row.outcome] ?? "chip"}>
                        {row.outcome}
                      </span>
                    </td>
                    <td className="mono px-3 py-2 text-xs text-fg-dim">
                      {row.target_ids?.map(String).join(" ") ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-xs text-danger">
                      {row.error ?? ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {audit !== null && audit.length > 0 && !exhausted && (
          <button
            type="button"
            className="btn mt-3"
            disabled={loadingMore}
            onClick={() => void loadMore()}
          >
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        )}
      </section>
    </div>
  );
}
