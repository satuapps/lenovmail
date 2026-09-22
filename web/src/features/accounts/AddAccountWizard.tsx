// Lenovmail — authored by satuapps (satuapps.com)
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../api";
import type { AccountCreate, DiscoverResult, ServerIn } from "../../types";

const SYNC_INTERVALS = [
  { value: 60, label: "1 minute" },
  { value: 300, label: "5 minutes" },
  { value: 900, label: "15 minutes" },
  { value: 3600, label: "1 hour" },
];

const SECURITY_OPTIONS: ServerIn["security"][] = ["ssl", "starttls", "none"];

interface ManualServer {
  host: string;
  port: string;
  security: ServerIn["security"];
  username: string;
  password: string;
}

const EMPTY_MANUAL: ManualServer = { host: "", port: "", security: "ssl", username: "", password: "" };

function manualFromDiscover(server: DiscoverResult["imap"] | DiscoverResult["smtp"]): ManualServer {
  if (!server) return EMPTY_MANUAL;
  const security: ServerIn["security"] =
    server.security === "ssl" || server.security === "starttls" || server.security === "none"
      ? server.security
      : "ssl";
  return {
    host: server.host,
    port: String(server.port),
    security,
    username: server.username ?? "",
    password: "",
  };
}

function manualToServerIn(manual: ManualServer): ServerIn {
  const port = Number.parseInt(manual.port, 10);
  return {
    host: manual.host,
    port: Number.isNaN(port) ? 0 : port,
    security: manual.security,
    username: manual.username || undefined,
    password: manual.password || undefined,
  };
}

function ManualServerFields({
  title,
  value,
  onChange,
}: {
  title: string;
  value: ManualServer;
  onChange: (next: ManualServer) => void;
}) {
  return (
    <div className="rounded-md border border-ink-600 p-3">
      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-fg-muted">{title}</div>
      <div className="grid grid-cols-2 gap-2">
        <div className="col-span-2 sm:col-span-1">
          <label className="label">Host</label>
          <input
            className="input"
            value={value.host}
            onChange={(e) => onChange({ ...value, host: e.target.value })}
          />
        </div>
        <div>
          <label className="label">Port</label>
          <input
            className="input"
            type="number"
            value={value.port}
            onChange={(e) => onChange({ ...value, port: e.target.value })}
          />
        </div>
        <div>
          <label className="label">Security</label>
          <select
            className="input"
            value={value.security}
            onChange={(e) => onChange({ ...value, security: e.target.value as ServerIn["security"] })}
          >
            {SECURITY_OPTIONS.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="label">Username</label>
          <input
            className="input"
            value={value.username}
            onChange={(e) => onChange({ ...value, username: e.target.value })}
          />
        </div>
        <div>
          <label className="label">Password</label>
          <input
            className="input"
            type="password"
            value={value.password}
            onChange={(e) => onChange({ ...value, password: e.target.value })}
          />
        </div>
      </div>
    </div>
  );
}

/** 3-step wizard for adding a new mail account: discovery, credentials, confirmation. */
export default function AddAccountWizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState<1 | 2 | 3>(1);

  const [email, setEmail] = useState("");
  const [discoverLoading, setDiscoverLoading] = useState(false);
  const [discoverError, setDiscoverError] = useState<string | null>(null);
  const [discoverResult, setDiscoverResult] = useState<DiscoverResult | null>(null);

  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [syncInterval, setSyncInterval] = useState(300);
  const [manualOpen, setManualOpen] = useState(false);
  const [imapManual, setImapManual] = useState<ManualServer>(EMPTY_MANUAL);
  const [smtpManual, setSmtpManual] = useState<ManualServer>(EMPTY_MANUAL);

  const [createLoading, setCreateLoading] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const oauthRequired = discoverResult?.oauth_required ?? false;

  async function handleDiscover() {
    setDiscoverError(null);
    setDiscoverLoading(true);
    try {
      const result = await api.discover(email);
      setDiscoverResult(result);
      setImapManual(manualFromDiscover(result.imap));
      setSmtpManual(manualFromDiscover(result.smtp));
      setStep(2);
    } catch (err) {
      setDiscoverError(err instanceof ApiError ? err.message : "Failed to detect configuration.");
    } finally {
      setDiscoverLoading(false);
    }
  }

  async function handleCreate() {
    setCreateError(null);
    setCreateLoading(true);
    const provider =
      discoverResult?.provider === "imap" || discoverResult?.provider === "graph"
        ? discoverResult.provider
        : "auto";
    const body: AccountCreate = {
      email_address: email,
      display_name: displayName || null,
      provider,
      password: oauthRequired ? null : password || null,
      imap: manualOpen ? manualToServerIn(imapManual) : null,
      smtp: manualOpen ? manualToServerIn(smtpManual) : null,
      sync_interval_s: syncInterval,
    };
    try {
      const account = await api.createAccount(body);
      if (account.oauth_url) {
        window.location.assign(account.oauth_url);
      } else {
        navigate(`/accounts/${account.id}`);
      }
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : "Failed to create account.");
    } finally {
      setCreateLoading(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl p-6">
      <h1 className="page-title mb-1">Add Mail Account</h1>
      <div className="mb-4 text-sm text-fg-muted">Step {step} of 3</div>

      {step === 1 && (
        <div className="card">
          <label className="label" htmlFor="wizard-email">
            Email address
          </label>
          <input
            id="wizard-email"
            type="email"
            className="input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
            placeholder="name@example.com"
          />
          {discoverError && (
            <div className="mt-3 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
              {discoverError}
            </div>
          )}
          <div className="mt-4 flex justify-end">
            <button
              type="button"
              className="btn btn-primary"
              disabled={!email || discoverLoading}
              onClick={() => void handleDiscover()}
            >
              {discoverLoading ? "Detecting…" : "Detect configuration"}
            </button>
          </div>
        </div>
      )}

      {step === 2 && discoverResult && (
        <div className="flex flex-col gap-4">
          <div className="card">
            <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
              <span className="chip">source: {discoverResult.source}</span>
              <span className="chip">provider: {discoverResult.provider}</span>
              {discoverResult.oauth_required && <span className="chip chip-warn">OAuth required</span>}
            </div>
            {discoverResult.imap && (
              <div className="text-xs text-fg-muted">
                IMAP detected: {discoverResult.imap.host}:{discoverResult.imap.port} (
                {discoverResult.imap.security})
              </div>
            )}
            {discoverResult.smtp && (
              <div className="text-xs text-fg-muted">
                SMTP detected: {discoverResult.smtp.host}:{discoverResult.smtp.port} (
                {discoverResult.smtp.security})
              </div>
            )}
            {discoverResult.warnings.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-xs text-warn">
                {discoverResult.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            )}
          </div>

          <div className="card">
            <div className="mb-3">
              <label className="label" htmlFor="wizard-display-name">
                Display name (optional)
              </label>
              <input
                id="wizard-display-name"
                className="input"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </div>

            <div className="mb-3">
              <label className="label" htmlFor="wizard-interval">
                Sync interval
              </label>
              <select
                id="wizard-interval"
                className="input"
                value={syncInterval}
                onChange={(e) => setSyncInterval(Number(e.target.value))}
              >
                {SYNC_INTERVALS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            {oauthRequired ? (
              <div className="rounded-md border border-accent/40 bg-accent/10 px-3 py-2 text-sm text-fg">
                This account uses Microsoft OAuth. The account will be created first, then a Microsoft
                consent window will open to connect the mailbox.
              </div>
            ) : (
              <div>
                <label className="label" htmlFor="wizard-password">
                  Password (or app password)
                </label>
                <input
                  id="wizard-password"
                  type="password"
                  className="input"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
            )}

            <button
              type="button"
              className="btn btn-ghost mt-3"
              onClick={() => setManualOpen((v) => !v)}
            >
              {manualOpen ? "Hide manual server settings" : "Configure manual server"}
            </button>

            {manualOpen && (
              <div className="mt-3 flex flex-col gap-3">
                <ManualServerFields title="IMAP" value={imapManual} onChange={setImapManual} />
                <ManualServerFields title="SMTP" value={smtpManual} onChange={setSmtpManual} />
              </div>
            )}
          </div>

          <div className="flex justify-between">
            <button type="button" className="btn" onClick={() => setStep(1)}>
              Back
            </button>
            <button type="button" className="btn btn-primary" onClick={() => setStep(3)}>
              Next
            </button>
          </div>
        </div>
      )}

      {step === 3 && discoverResult && (
        <div className="flex flex-col gap-4">
          <div className="card">
            <div className="mb-2 text-sm font-medium text-fg">Summary</div>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-fg-muted">Email</dt>
              <dd className="text-fg">{email}</dd>
              <dt className="text-fg-muted">Provider</dt>
              <dd className="text-fg">{discoverResult.provider}</dd>
              <dt className="text-fg-muted">Display name</dt>
              <dd className="text-fg">{displayName || "—"}</dd>
              <dt className="text-fg-muted">Sync interval</dt>
              <dd className="text-fg">
                {SYNC_INTERVALS.find((o) => o.value === syncInterval)?.label ?? `${syncInterval}s`}
              </dd>
              <dt className="text-fg-muted">Manual server</dt>
              <dd className="text-fg">{manualOpen ? "yes, sent to server" : "no, auto-detected"}</dd>
              <dt className="text-fg-muted">Authentication</dt>
              <dd className="text-fg">{oauthRequired ? "Microsoft OAuth" : "password"}</dd>
            </dl>
          </div>

          {createError && (
            <div className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
              {createError}
            </div>
          )}

          <div className="flex justify-between">
            <button type="button" className="btn" onClick={() => setStep(2)} disabled={createLoading}>
              Back
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={createLoading}
              onClick={() => void handleCreate()}
            >
              {createLoading ? "Creating account…" : "Create account"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
