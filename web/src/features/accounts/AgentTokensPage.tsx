// Lenovmail — authored by satuapps
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import { fullDate, relativeTime } from "../../format";
import { AGENT_SCOPES } from "../../types";
import type { Account, AgentAuditEntry, AgentToken, AgentTokenCreated } from "../../types";

const OUTCOME_CHIP: Record<string, string> = {
  ok: "chip chip-ok",
  denied: "chip chip-warn",
  error: "chip chip-danger",
};

/** AI agent tokens page: create/revoke scoped tokens, and view the audit log. */
export default function AgentTokensPage() {
  const [tokens, setTokens] = useState<AgentToken[] | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [audit, setAudit] = useState<AgentAuditEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>([]);
  const [accountIds, setAccountIds] = useState<string[]>([]);
  const [requireApproval, setRequireApproval] = useState(true);
  const [sendLimit, setSendLimit] = useState(20);
  const [expiresInDays, setExpiresInDays] = useState<string>("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createdToken, setCreatedToken] = useState<AgentTokenCreated | null>(null);
  const [copied, setCopied] = useState(false);

  const [revokingId, setRevokingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [tokenList, accountList, auditList] = await Promise.all([
        api.listTokens(),
        api.listAccounts(),
        api.listAudit(200),
      ]);
      setTokens(tokenList);
      setAccounts(accountList);
      setAudit(auditList);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Failed to load token data.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function toggleScope(scope: string) {
    setScopes((prev) => (prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope]));
  }

  async function handleCreate() {
    setCreateError(null);
    setCreating(true);
    try {
      const created = await api.createToken({
        name,
        scopes,
        account_ids: accountIds.length > 0 ? accountIds : null,
        require_send_approval: requireApproval,
        send_limit_per_hour: sendLimit,
        expires_in_days: expiresInDays ? Number.parseInt(expiresInDays, 10) : null,
      });
      setCreatedToken(created);
      setCopied(false);
      setName("");
      setScopes([]);
      setAccountIds([]);
      setRequireApproval(true);
      setSendLimit(20);
      setExpiresInDays("");
      await load();
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : "Failed to create token.");
    } finally {
      setCreating(false);
    }
  }

  async function handleRevoke(token: AgentToken) {
    if (!window.confirm(`Revoke token "${token.name}"? Agents using it will be denied immediately.`)) {
      return;
    }
    setRevokingId(token.id);
    try {
      await api.revokeToken(token.id);
      await load();
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Failed to revoke token.");
    } finally {
      setRevokingId(null);
    }
  }

  async function handleCopy() {
    if (!createdToken) return;
    await navigator.clipboard.writeText(createdToken.token);
    setCopied(true);
  }

  return (
    <div className="mx-auto max-w-4xl p-6">
      <h1 className="mb-4 text-lg font-semibold text-fg">Agent Tokens</h1>

      {loadError && (
        <div className="mb-4 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
          {loadError}
        </div>
      )}

      <div className="mb-4 card border-accent/40 bg-accent/5 text-sm text-fg-muted">
        AI agents use this token as the <code className="mono">Authorization: Bearer &lt;token&gt;</code>{" "}
        header to call the REST <code className="mono">/api/...</code> API and the MCP{" "}
        <code className="mono">/api/mcp</code> endpoint.
      </div>

      {createdToken && (
        <div className="mb-4 card border-warn/40 bg-warn/5">
          <div className="mb-2 text-sm font-medium text-warn">
            Token created — copy it now, it will not be shown again.
          </div>
          <div className="flex items-center gap-2">
            <code className="mono flex-1 overflow-x-auto rounded-md bg-ink-900 px-3 py-2 text-sm text-fg">
              {createdToken.token}
            </code>
            <button type="button" className="btn" onClick={() => void handleCopy()}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
      )}

      <div className="mb-6 card">
        <div className="mb-3 text-sm font-medium text-fg">New token</div>
        <div className="mb-3">
          <label className="label" htmlFor="token-name">
            Name
          </label>
          <input id="token-name" className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>

        <div className="mb-3">
          <div className="label">Scope</div>
          <div className="flex flex-wrap gap-3">
            {AGENT_SCOPES.map((scope) => (
              <label key={scope} className="flex items-center gap-1.5 text-sm text-fg">
                <input
                  type="checkbox"
                  checked={scopes.includes(scope)}
                  onChange={() => toggleScope(scope)}
                />
                {scope}
              </label>
            ))}
          </div>
        </div>

        <div className="mb-3">
          <label className="label" htmlFor="token-accounts">
            Account scope (leave empty for all accounts)
          </label>
          <select
            id="token-accounts"
            className="input"
            multiple
            value={accountIds}
            onChange={(e) => setAccountIds(Array.from(e.target.selectedOptions, (o) => o.value))}
          >
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.email_address}
              </option>
            ))}
          </select>
        </div>

        <div className="mb-3 grid grid-cols-2 gap-3">
          <div>
            <label className="label" htmlFor="token-limit">
              Send limit / hour
            </label>
            <input
              id="token-limit"
              type="number"
              min={0}
              className="input"
              value={sendLimit}
              onChange={(e) => setSendLimit(Number(e.target.value))}
            />
          </div>
          <div>
            <label className="label" htmlFor="token-expiry">
              Expiry (days, leave empty for no expiry)
            </label>
            <input
              id="token-expiry"
              type="number"
              min={1}
              className="input"
              value={expiresInDays}
              onChange={(e) => setExpiresInDays(e.target.value)}
            />
          </div>
        </div>

        <label className="mb-3 flex items-center gap-1.5 text-sm text-fg">
          <input
            type="checkbox"
            checked={requireApproval}
            onChange={(e) => setRequireApproval(e.target.checked)}
          />
          Require approval before sending
        </label>

        {createError && (
          <div className="mb-3 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            {createError}
          </div>
        )}

        <button
          type="button"
          className="btn btn-primary"
          disabled={creating || !name || scopes.length === 0}
          onClick={() => void handleCreate()}
        >
          {creating ? "Creating…" : "Create token"}
        </button>
      </div>

      <div className="mb-6 card">
        <div className="mb-3 text-sm font-medium text-fg">Active tokens</div>
        {tokens === null && <div className="text-sm text-fg-muted">Loading…</div>}
        {tokens && tokens.length === 0 && <div className="text-sm text-fg-muted">No tokens yet.</div>}
        {tokens && tokens.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-fg-dim">
                <th className="pb-1">Name</th>
                <th className="pb-1">Scope</th>
                <th className="pb-1">Account</th>
                <th className="pb-1">Approval</th>
                <th className="pb-1">Limit/hour</th>
                <th className="pb-1">Expiry</th>
                <th className="pb-1">Last used</th>
                <th className="pb-1" />
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => (
                <tr key={token.id} className="border-t border-ink-600 align-top">
                  <td className="py-1.5 text-fg">
                    {token.name}
                    {token.revoked_at && <span className="chip chip-danger ml-2">revoked</span>}
                  </td>
                  <td className="py-1.5">
                    <div className="flex flex-wrap gap-1">
                      {token.scopes.map((scope) => (
                        <span key={scope} className="chip">
                          {scope}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="py-1.5 text-fg-muted">
                    {token.account_ids === null
                      ? "all accounts"
                      : token.account_ids
                          .map((id) => accounts.find((a) => a.id === id)?.email_address ?? id)
                          .join(", ") || "—"}
                  </td>
                  <td className="py-1.5 text-fg-muted">{token.require_send_approval ? "yes" : "no"}</td>
                  <td className="py-1.5 text-fg-muted">{token.send_limit_per_hour}</td>
                  <td className="py-1.5 text-fg-muted">{fullDate(token.expires_at)}</td>
                  <td className="py-1.5 text-fg-muted">{relativeTime(token.last_used_at)}</td>
                  <td className="py-1.5">
                    {!token.revoked_at && (
                      <button
                        type="button"
                        className="btn btn-danger"
                        disabled={revokingId === token.id}
                        onClick={() => void handleRevoke(token)}
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <div className="mb-3 text-sm font-medium text-fg">Audit</div>
        {audit === null && <div className="text-sm text-fg-muted">Loading…</div>}
        {audit && audit.length === 0 && <div className="text-sm text-fg-muted">No activity yet.</div>}
        {audit && audit.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-fg-dim">
                <th className="pb-1">Time</th>
                <th className="pb-1">Tool</th>
                <th className="pb-1">Outcome</th>
                <th className="pb-1">Error</th>
                <th className="pb-1">Target</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((entry) => (
                <tr key={entry.id} className="border-t border-ink-600">
                  <td className="py-1.5 text-fg-muted">{fullDate(entry.created_at)}</td>
                  <td className="py-1.5 text-fg mono">{entry.tool}</td>
                  <td className="py-1.5">
                    <span className={OUTCOME_CHIP[entry.outcome] ?? "chip"}>{entry.outcome}</span>
                  </td>
                  <td className="py-1.5 text-xs text-danger">{entry.error ?? ""}</td>
                  <td className="py-1.5 text-xs text-fg-dim mono">
                    {entry.target_ids ? entry.target_ids.map(String).join(", ") : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
