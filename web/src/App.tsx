import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "./auth/auth-context";
import { ConnectionDialog } from "./components/ConnectionDialog";
import { OverviewPage } from "./pages/OverviewPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { WorkspacePage } from "./pages/WorkspacePage";

type Page = "overview" | "workspace" | "admin";

const pages: Record<Page, { label: string; description: string }> = {
  overview: { label: "Overview", description: "Runtime health and delivery status" },
  workspace: { label: "Workspace", description: "Threads and agent runs" },
  admin: { label: "Administration", description: "Tenant operations and governance" },
};

export function App() {
  const [page, setPage] = useState<Page>("overview");
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const { authentication, setAuthentication } = useAuth();
  const queryClient = useQueryClient();

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
          {(Object.keys(pages) as Page[]).map((key) => (
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
          <button className="connection-button" onClick={() => setConnectionOpen(true)}>
            <span className="connection-indicator" /><span>{authLabel(authentication.mode)}</span><small>Configure</small>
          </button>
        </header>
        <main id="main-content">
          {page === "overview" && <OverviewPage />}
          {page === "workspace" && <WorkspacePage />}
          {page === "admin" && <PlaceholderPage eyebrow="Administration" title="Operate every tenant with confidence." copy="The admin surface will use Minigent’s existing tenant APIs with explicit validation, auditability, and safe destructive actions." features={["Tenant, user, domain, and entitlement management", "Execution configuration validation", "Thread, attachment, concurrency, and audit inspection"]} />}
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

function authLabel(mode: "session" | "development" | "bearer") {
  if (mode === "development") return "Development";
  if (mode === "bearer") return "Bearer token";
  return "Secure session";
}

function NavIcon({ page }: { page: Page }) {
  if (page === "workspace") return <span aria-hidden="true">◇</span>;
  if (page === "admin") return <span aria-hidden="true">⌘</span>;
  return <span aria-hidden="true">◫</span>;
}
