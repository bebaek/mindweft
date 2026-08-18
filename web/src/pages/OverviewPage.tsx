import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../auth/auth-context";

export function OverviewPage() {
  const { api, authentication } = useAuth();
  const readiness = useQuery({
    queryKey: ["readiness"],
    queryFn: ({ signal }) => api.getReadiness(signal),
    refetchInterval: 30_000,
  });
  const executionOptions = useQuery({
    queryKey: ["execution-options", authentication],
    queryFn: ({ signal }) => api.getExecutionOptions(signal),
    retry: false,
  });

  const checks = Object.entries(readiness.data?.checks ?? {});
  const optionCount = executionOptions.data
    ? executionOptions.data.skills.items.length +
      executionOptions.data.capability_profiles.items.length +
      executionOptions.data.llm_profiles.items.length
    : null;

  return (
    <div className="page-stack">
      <section className="hero-card">
        <div>
          <p className="eyebrow">Workspace overview</p>
          <h1>Build, observe, and govern your agents.</h1>
          <p className="hero-copy">A focused control plane for conversations, runtime activity, and tenant operations.</p>
        </div>
        <div className={`readiness-orb ${readiness.data?.status === "ready" ? "is-ready" : ""}`} aria-hidden="true"><span /></div>
      </section>

      <section className="metric-grid" aria-label="Runtime summary">
        <article className="metric-card">
          <span>API status</span>
          <strong>{readiness.isPending ? "Checking" : readiness.data?.status === "ready" ? "Ready" : "Attention"}</strong>
          <small>Updated automatically every 30 seconds</small>
        </article>
        <article className="metric-card">
          <span>Runtime checks</span>
          <strong>{checks.length || "—"}</strong>
          <small>{checks.filter(([, value]) => value === "ok").length} checks passing</small>
        </article>
        <article className="metric-card">
          <span>Execution options</span>
          <strong>{optionCount ?? "—"}</strong>
          <small>{executionOptions.data ? `Tenant ${executionOptions.data.tenant_id}` : "Connect to load tenant settings"}</small>
        </article>
      </section>

      <section className="content-grid">
        <article className="panel">
          <div className="panel-heading"><div><p className="eyebrow">Infrastructure</p><h2>Readiness checks</h2></div><span className="live-label"><i /> Live</span></div>
          {readiness.isError ? (
            <p className="error-state">The Mindweft API could not be reached.</p>
          ) : checks.length ? (
            <ul className="check-list">{checks.map(([name, value]) => <li key={name}><span className={`status-dot ${value === "ok" ? "ok" : "failed"}`} /><span>{name.replaceAll("_", " ")}</span><strong>{value}</strong></li>)}</ul>
          ) : (
            <div className="skeleton-list" aria-label="Loading readiness checks"><i /><i /><i /></div>
          )}
        </article>

        <article className="panel next-panel">
          <p className="eyebrow">Delivery plan</p>
          <h2>Foundation in place</h2>
          <ol className="roadmap">
            <li className="complete"><span>01</span><div><strong>Application shell</strong><small>TypeScript, query lifecycle, secure CSP build</small></div></li>
            <li><span>02</span><div><strong>Conversation workspace</strong><small>Threads, streaming runs, activity, attachments</small></div></li>
            <li><span>03</span><div><strong>Administration</strong><small>Tenants, users, configuration, audit</small></div></li>
          </ol>
        </article>
      </section>
    </div>
  );
}
