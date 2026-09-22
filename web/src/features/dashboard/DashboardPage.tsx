// Lenovmail — authored by satuapps (satuapps.com)
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../../api";
import { useEventStream } from "../../events";
import type { Overview } from "../../types";

interface Tile {
  label: string;
  value: number;
  /** Where the number can be acted on, if anywhere. */
  to?: string;
  tone?: "ok" | "warn" | "danger";
}

const TONE_CLASS: Record<string, string> = {
  ok: "text-ok",
  warn: "text-warn",
  danger: "text-danger",
};

/** Landing page: account health, message totals, and the per-provider breakdown. */
export default function DashboardPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      setOverview(await api.statsOverview());
    } catch (err) {
      setLoadError(
        err instanceof ApiError ? err.message : "Failed to load dashboard.",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEventStream((event) => {
    // Counts move when an account changes state or a sync finishes; nothing else shifts them.
    if (event.type === "account.status" || event.type === "sync.progress")
      void load();
  });

  const tiles: Tile[] = overview
    ? [
        { label: "Accounts", value: overview.accounts_total, to: "/accounts" },
        { label: "Active", value: overview.accounts_active, tone: "ok" },
        {
          label: "Unreachable",
          value: overview.accounts_unreachable,
          to: "/ops",
          tone: "warn",
        },
        {
          label: "Auth rejected",
          value: overview.accounts_auth_rejected,
          to: "/ops",
          tone: "danger",
        },
        {
          label: "Disconnected",
          value: overview.accounts_disabled,
          to: "/accounts",
        },
        { label: "Messages", value: overview.messages_total, to: "/mail" },
        { label: "Unread", value: overview.unread_total, to: "/mail" },
      ]
    : [];

  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="page-title">Dashboard</h1>
        <button type="button" className="btn" onClick={() => void load()}>
          Refresh
        </button>
      </div>

      {loadError && (
        <div className="mb-4 flex items-center gap-3">
          <span className="chip chip-danger">{loadError}</span>
          <button type="button" className="btn" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}

      {overview === null && !loadError && (
        <div className="text-sm text-fg-muted">Loading…</div>
      )}

      {overview !== null && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            {tiles.map((tile) => {
              const body = (
                <>
                  <div className="label">{tile.label}</div>
                  <div
                    className={`mono text-2xl ${tile.value > 0 && tile.tone ? TONE_CLASS[tile.tone] : "text-fg"}`}
                  >
                    {tile.value}
                  </div>
                </>
              );
              return tile.to ? (
                <Link
                  key={tile.label}
                  to={tile.to}
                  className="card transition-colors hover:border-accent/40"
                >
                  {body}
                </Link>
              ) : (
                <div key={tile.label} className="card">
                  {body}
                </div>
              );
            })}
          </div>

          <h2 className="page-title mt-6 mb-2">By provider</h2>
          <div className="panel overflow-x-auto rounded-xl">
            <table className="w-full text-left text-sm">
              <thead className="text-[11px] uppercase tracking-[0.14em] text-fg-dim">
                <tr>
                  <th className="px-3 py-2 font-medium">Provider</th>
                  <th className="px-3 py-2 font-medium">Accounts</th>
                  <th className="px-3 py-2 font-medium">Active</th>
                  <th className="px-3 py-2 font-medium">Unreachable</th>
                  <th className="px-3 py-2 font-medium">Auth rejected</th>
                  <th className="px-3 py-2 font-medium">Disconnected</th>
                  <th className="px-3 py-2 font-medium">Messages</th>
                  <th className="px-3 py-2 font-medium">Retiring</th>
                </tr>
              </thead>
              <tbody className="mono">
                {overview.providers.map((row) => (
                  <tr key={row.provider} className="border-t border-ink-600">
                    <td className="px-3 py-2 text-fg">{row.provider}</td>
                    <td className="px-3 py-2 text-fg-muted">
                      {row.accounts_total}
                    </td>
                    <td className="px-3 py-2 text-fg-muted">
                      {row.accounts_active}
                    </td>
                    <td
                      className={
                        row.accounts_unreachable > 0
                          ? "px-3 py-2 text-warn"
                          : "px-3 py-2 text-fg-muted"
                      }
                    >
                      {row.accounts_unreachable}
                    </td>
                    <td
                      className={
                        row.accounts_auth_rejected > 0
                          ? "px-3 py-2 text-danger"
                          : "px-3 py-2 text-fg-muted"
                      }
                    >
                      {row.accounts_auth_rejected}
                    </td>
                    <td className="px-3 py-2 text-fg-muted">
                      {row.accounts_disabled}
                    </td>
                    <td className="px-3 py-2 text-fg-muted">{row.messages}</td>
                    <td className="px-3 py-2">
                      {row.retire_warnings > 0 ? (
                        <span className="chip chip-warn">
                          {row.retire_warnings} retiring
                        </span>
                      ) : (
                        <span className="text-fg-dim">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
