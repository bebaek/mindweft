import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "./auth/auth-context";
import { ConnectionDialog } from "./components/ConnectionDialog";
import { OverviewPage } from "./pages/OverviewPage";
import { WorkspacePage } from "./pages/WorkspacePage";
import { AdminPage } from "./pages/AdminPage";
import { LoginPage } from "./pages/LoginPage";
import { PasswordSetupPage } from "./pages/PasswordSetupPage";

type Page = "overview" | "workspace" | "settings" | "admin";

const pages: Record<Page, { label: string; description: string }> = {
  overview: { label: "Overview", description: "Runtime health and delivery status" },
  workspace: { label: "Workspace", description: "Threads and agent runs" },
  settings: { label: "Tenant settings", description: "Members, domains, and runtime configuration" },
  admin: { label: "Administration", description: "Tenant operations and governance" },
};

export function App() {
  const [page, setPage] = useState<Page>("overview");
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const { api, authentication, setAuthentication, session, logout } = useAuth();
  const queryClient = useQueryClient();
  const setupToken = passwordSetupToken();
  const tenantContext = useQuery({
    queryKey: ["tenant-context", authentication],
    queryFn: ({ signal }) => api.getTenantContext(signal),
    enabled: authentication.mode !== "session" || session.authenticated,
    retry: false,
  });
  const platformAdmin = tenantContext.data?.principal.is_admin === true
    || session.principal?.is_admin === true
    || (authentication.mode === "development" && authentication.isAdmin);
  const tenantOwner = tenantContext.data?.user_role === "owner";
  const visiblePages = (Object.keys(pages) as Page[]).filter((key) =>
    key !== "admin" || platformAdmin,
  ).filter((key) => key !== "settings" || tenantOwner);

  if (setupToken) return <PasswordSetupPage token={setupToken} />;
  if (authentication.mode === "session" && session.loading) {
    return <main className="login-page"><p className="session-loading">Checking secure session…</p></main>;
  }
  if (authentication.mode === "session" && session.enabled && !session.authenticated) {
    return <LoginPage />;
  }

  function navigate(nextPage: Page) {
    setPage(nextPage);
    setMobileNavOpen(false);
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className={`sidebar ${mobileNavOpen ? "is-open" : ""}`}>
        <div className="brand"><span className="brand-mark">M</span><div><strong>Minigent</strong><small>Agent operations</small></div></div>
        <nav aria-label="Primary navigation">
          {visiblePages.map((key) => (
            <button key={key} className={page === key ? "active" : ""} onClick={() => navigate(key)}>
              <NavIcon page={key} /><span>{pages[key].label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-footer"><span className="environment-dot" /><div><strong>Minigent API</strong><small>Same-origin connection</small></div></div>
      </aside>
      {mobileNavOpen && <button className="nav-backdrop" aria-label="Close navigation" onClick={() => setMobileNavOpen(false)} />}

      <div className="main-column">
        <header className="topbar">
          <button className="menu-button" onClick={() => setMobileNavOpen(true)} aria-label="Open navigation">☰</button>
          <div><strong>{pages[page].label}</strong><small>{pages[page].description}</small></div>
          <button
            className="connection-button"
            onClick={() => {
              if (authentication.mode === "session" && session.authenticated) {
                void logout().then(() => queryClient.clear());
              } else {
                setConnectionOpen(true);
              }
            }}
          >
            <span className="connection-indicator" />
            <span>{sessionLabel(authentication.mode, session.principal?.user_id)}</span>
            <small>{authentication.mode === "session" && session.authenticated ? "Sign out" : "Configure"}</small>
          </button>
        </header>
        <main id="main-content">
          {page === "overview" && <OverviewPage />}
          {page === "workspace" && <WorkspacePage />}
          {page === "settings" && tenantOwner && tenantContext.data && <AdminPage tenantId={tenantContext.data.tenant_id} />}
          {page === "admin" && platformAdmin && <AdminPage />}
        </main>
      </div>

      <ConnectionDialog
        open={connectionOpen}
        authentication={authentication}
        onClose={() => setConnectionOpen(false)}
        onSave={(nextAuthentication) => {
          setAuthentication(nextAuthentication);
          void queryClient.invalidateQueries();
        }}
      />
    </div>
  );
}

function passwordSetupToken(): string | null {
  const match = /^#setup=(.+)$/.exec(window.location.hash);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function sessionLabel(mode: "session" | "development" | "bearer", userId?: string) {
  if (mode === "session" && userId) return userId;
  return authLabel(mode);
}

function authLabel(mode: "session" | "development" | "bearer") {
  if (mode === "development") return "Development";
  if (mode === "bearer") return "Bearer token";
  return "Secure session";
}

function NavIcon({ page }: { page: Page }) {
  if (page === "workspace") return <span aria-hidden="true">◇</span>;
  if (page === "settings") return <span aria-hidden="true">⚙</span>;
  if (page === "admin") return <span aria-hidden="true">⌘</span>;
  return <span aria-hidden="true">◫</span>;
}
