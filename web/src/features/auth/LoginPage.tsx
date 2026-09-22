// Lenovmail — authored by satuapps (satuapps.com)
import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "../../api";
import { useSession } from "../../session";

/** Login page: centered card with email + password. */
export default function LoginPage() {
  const { login } = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);
    try {
      await login(email, password);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to sign in. Try again.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="grid h-full place-items-center bg-ink-900 px-4">
      <form onSubmit={(e) => void handleSubmit(e)} className="card w-full max-w-sm">
        <div className="mb-4 text-center">
          <div className="mono text-lg font-semibold text-fg">lenovmail</div>
          <div className="text-sm text-fg-muted">Sign in to manage your mail</div>
        </div>

        {error && (
          <div className="mb-4 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </div>
        )}

        <div className="mb-3">
          <label className="label" htmlFor="login-email">
            Email
          </label>
          <input
            id="login-email"
            type="email"
            className="input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
            autoComplete="username"
            required
            disabled={pending}
          />
        </div>

        <div className="mb-4">
          <label className="label" htmlFor="login-password">
            Password
          </label>
          <input
            id="login-password"
            type="password"
            className="input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
            disabled={pending}
          />
        </div>

        <button type="submit" className="btn btn-primary w-full justify-center" disabled={pending}>
          {pending ? "Processing…" : "Sign in"}
        </button>

        <p className="mt-4 text-center text-xs text-fg-dim">
          No admin account yet? Create one via the CLI:{" "}
          <code className="mono">uv run lenovmail bootstrap-admin --email admin@example.com</code>
        </p>
      </form>
    </div>
  );
}
