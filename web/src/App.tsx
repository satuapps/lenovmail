// Lenovmail — authored by satuapps (satuapps.com)
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { BrowserRouter } from "react-router-dom";
import { EventsProvider, useStreamStatus } from "./events";
import { SessionProvider, useSession } from "./session";
import LoginPage from "./features/auth/LoginPage";
import PasswordPage from "./features/auth/PasswordPage";
import AccountsPage from "./features/accounts/AccountsPage";
import AccountDetailPage from "./features/accounts/AccountDetailPage";
import AddAccountWizard from "./features/accounts/AddAccountWizard";
import AgentTokensPage from "./features/accounts/AgentTokensPage";
import MailPage from "./features/mail/MailPage";
import OutboxPage from "./features/mail/OutboxPage";

const NAV = [
  { to: "/mail", label: "Mail" },
  { to: "/accounts", label: "Accounts" },
  { to: "/outbox", label: "Outbox" },
  { to: "/agent", label: "Agent Tokens" },
];

const STATUS_LABEL: Record<string, string> = {
  open: "live",
  connecting: "connecting",
  closed: "disconnected",
};

function Shell() {
  const { user, loading, logout } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const streamStatus = useStreamStatus();

  if (loading) {
    return <div className="grid h-full place-items-center text-fg-muted">Loading…</div>;
  }
  if (user === null) return <Navigate to="/login" replace />;

  return (
    <div className="flex h-full flex-col">
      <header className="relative flex items-center gap-4 border-b border-ink-600 bg-ink-800 px-4 py-2">
        <span
          className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent-strong/70 to-transparent"
          aria-hidden
        />
        <a
          href="https://satuapps.com/lenovmail"
          target="_blank"
          rel="noreferrer"
          className="flex items-baseline gap-2"
        >
          <span className="mono text-sm font-semibold uppercase tracking-[0.22em] text-fg">
            lenovmail
          </span>
          <span className="mono hidden text-[11px] tracking-[0.12em] text-fg-dim sm:inline">
            satuapps.com
          </span>
        </a>
        <nav className="flex items-center gap-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-link ${isActive ? "nav-link-active" : ""}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-3">
          <span
            className={streamStatus === "open" ? "chip chip-ok" : "chip chip-warn"}
            title="Realtime event stream from the server"
          >
            <span className={streamStatus === "open" ? "led led-ok" : "led led-warn"} aria-hidden />
            {STATUS_LABEL[streamStatus]}
          </span>
          <NavLink to="/password" className="mono text-xs text-fg-muted hover:text-fg">
            {user.email}
          </NavLink>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              void (async () => {
                await logout();
                navigate("/login", { replace: true });
              })();
            }}
          >
            Sign out
          </button>
        </div>
      </header>
      <main className="hud-grid min-h-0 flex-1">
        <Routes>
          <Route path="/mail" element={<MailPage />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/accounts/new" element={<AddAccountWizard />} />
          <Route path="/accounts/:accountId" element={<AccountDetailPage />} />
          <Route path="/outbox" element={<OutboxPage />} />
          <Route path="/agent" element={<AgentTokensPage />} />
          <Route path="/password" element={<PasswordPage />} />
          <Route
            path="*"
            element={<Navigate to={{ pathname: "/mail", search: location.search }} replace />}
          />
        </Routes>
      </main>
    </div>
  );
}

function LoginRoute() {
  const { user, loading } = useSession();
  if (loading) return <div className="grid h-full place-items-center text-fg-muted">Loading…</div>;
  if (user !== null) return <Navigate to="/mail" replace />;
  return <LoginPage />;
}

export default function App() {
  return (
    <BrowserRouter>
      <SessionProvider>
        <Routes>
          <Route path="/login" element={<LoginRoute />} />
          <Route
            path="*"
            element={
              <EventsProvider>
                <Shell />
              </EventsProvider>
            }
          />
        </Routes>
      </SessionProvider>
    </BrowserRouter>
  );
}
