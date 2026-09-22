// Lenovmail — authored by satuapps
import { useState } from "react";
import type { FormEvent } from "react";
import { api, ApiError } from "../../api";

const MIN_LENGTH = 10;

/** Change-password page for the currently logged-in user. */
export default function PasswordPage() {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSuccess(false);
    if (newPassword.length < MIN_LENGTH) {
      setError(`New password must be at least ${MIN_LENGTH} characters.`);
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("Password confirmation doesn't match.");
      return;
    }
    setError(null);
    setPending(true);
    try {
      await api.changePassword(currentPassword, newPassword);
      setSuccess(true);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to change password.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="mx-auto max-w-md p-6">
      <h1 className="mb-4 text-lg font-semibold text-fg">Change Password</h1>
      <form onSubmit={(e) => void handleSubmit(e)} className="card">
        {error && (
          <div className="mb-4 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </div>
        )}
        {success && (
          <div className="mb-4 rounded-md border border-ok/40 bg-ok/10 px-3 py-2 text-sm text-ok">
            Password changed successfully.
          </div>
        )}

        <div className="mb-3">
          <label className="label" htmlFor="pw-current">
            Current password
          </label>
          <input
            id="pw-current"
            type="password"
            className="input"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            autoComplete="current-password"
            required
            disabled={pending}
          />
        </div>

        <div className="mb-3">
          <label className="label" htmlFor="pw-new">
            New password
          </label>
          <input
            id="pw-new"
            type="password"
            className="input"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
            required
            disabled={pending}
          />
          <p className="mt-1 text-xs text-fg-dim">At least {MIN_LENGTH} characters.</p>
        </div>

        <div className="mb-4">
          <label className="label" htmlFor="pw-confirm">
            Confirm new password
          </label>
          <input
            id="pw-confirm"
            type="password"
            className="input"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            autoComplete="new-password"
            required
            disabled={pending}
          />
        </div>

        <button type="submit" className="btn btn-primary" disabled={pending}>
          {pending ? "Saving…" : "Save password"}
        </button>
      </form>
    </div>
  );
}
