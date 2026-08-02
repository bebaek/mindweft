import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TenantStatus } from "../api/client";
import { useAuth } from "../auth/auth-context";

export function AdminPage() {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [selection, setSelection] = useState<string | null>(null);
  const [pendingStatus, setPendingStatus] = useState<"active" | "suspended" | "archived" | null>(null);
  const tenants = useQuery({
    queryKey: ["admin-tenants", authentication],
    queryFn: ({ signal }) => api.listAdminTenants(signal),
    retry: false,
  });
  const filteredTenants = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return tenants.data?.tenants ?? [];
    return (tenants.data?.tenants ?? []).filter((tenant) =>
      [tenant.name, tenant.slug, tenant.id, tenant.plan, tenant.region]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    );
  }, [search, tenants.data]);
  const tenantId = selection ?? filteredTenants[0]?.id ?? null;
  const tenant = tenants.data?.tenants.find((item) => item.id === tenantId) ?? null;

  const users = useQuery({
    queryKey: ["admin-tenant-users", tenantId, authentication],
    queryFn: ({ signal }) => api.listAdminTenantUsers(tenantId, signal),
    enabled: tenantId !== null,
    retry: false,
  });
  const domains = useQuery({
    queryKey: ["admin-tenant-domains", tenantId, authentication],
    queryFn: ({ signal }) => api.listAdminTenantDomains(tenantId, signal),
    enabled: tenantId !== null,
    retry: false,
  });
  const attachments = useQuery({
    queryKey: ["admin-attachment-statistics", tenantId, authentication],
    queryFn: ({ signal }) => api.getAdminAttachmentStatistics(tenantId, signal),
    enabled: tenantId !== null,
    retry: false,
  });
  const concurrency = useQuery({
    queryKey: ["admin-run-concurrency", tenantId, authentication],
    queryFn: ({ signal }) => api.getAdminRunConcurrency(tenantId, signal),
    enabled: tenantId !== null,
    retry: false,
  });
  const transition = useMutation({
    mutationFn: (status: "active" | "suspended" | "archived") =>
      api.transitionAdminTenant(tenantId, status),
    onSuccess: () => {
      setPendingStatus(null);
      void queryClient.invalidateQueries({ queryKey: ["admin-tenants"] });
    },
  });

  return (
    <section className="admin-page">
      <header className="admin-heading">
        <div><p className="eyebrow">Administration</p><h1>Tenant operations</h1><p>Inspect customer environments, capacity, membership, and lifecycle state.</p></div>
        <div className="tenant-total"><strong>{tenants.data?.total ?? "—"}</strong><span>Total tenants</span></div>
      </header>

      {tenants.isError && (
        <div className="admin-access-error" role="alert">
          <strong>Tenant administration is unavailable.</strong>
          <span>Confirm the admin principal and configured administration store, then retry.</span>
        </div>
      )}

      <div className="admin-layout">
        <aside className="tenant-directory" aria-label="Tenant directory">
          <label className="tenant-search"><span className="sr-only">Search tenants</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search tenants…" /></label>
          <div className="tenant-directory-list">
            {filteredTenants.map((item) => (
              <button aria-label={item.name} key={item.id} type="button" className={item.id === tenantId ? "active" : ""} onClick={() => { setSelection(item.id); setPendingStatus(null); }}>
                <span className="tenant-avatar">{initials(item.name)}</span>
                <span><strong>{item.name}</strong><small>{item.slug}</small></span>
                <StatusPill status={item.status} />
              </button>
            ))}
            {!tenants.isPending && filteredTenants.length === 0 && <p>No matching tenants.</p>}
          </div>
        </aside>

        <div className="tenant-detail">
          {tenant ? (
            <>
              <section className="tenant-summary">
                <div><span className="tenant-avatar large">{initials(tenant.name)}</span><div><p className="eyebrow">{tenant.id}</p><h2>{tenant.name}</h2><p>{tenant.plan || "No plan"} · {tenant.region || "No region"}</p></div></div>
                <StatusPill status={tenant.status} />
              </section>

              <section className="admin-metrics" aria-label="Tenant metrics">
                <Metric label="Members" value={users.data?.total} detail={`${activeUsers(users.data?.users)} active`} />
                <Metric label="Active runs" value={concurrency.data?.active_runs} detail={`${concurrency.data?.tenant_capacity ?? "—"} tenant capacity`} />
                <Metric label="Attachments" value={attachments.data?.total_count} detail={formatBytes(attachments.data?.total_bytes)} />
                <Metric label="Domains" value={domains.data?.length} detail={`${domains.data?.filter((domain) => domain.verified).length ?? 0} verified`} />
              </section>

              <section className="admin-detail-grid">
                <article className="admin-panel">
                  <div className="admin-panel-heading"><div><p className="eyebrow">Membership</p><h3>Users</h3></div><span>{users.data?.total ?? 0}</span></div>
                  <ul className="admin-user-list">
                    {users.data?.users.slice(0, 6).map((user) => <li key={user.id}><span className="user-avatar">{initials(user.display_name || user.user_id)}</span><div><strong>{user.display_name || user.user_id}</strong><small>{user.email || user.user_id}</small></div><span>{user.role}</span></li>)}
                    {!users.isPending && !users.data?.users.length && <li className="admin-empty">No users registered.</li>}
                  </ul>
                </article>
                <article className="admin-panel">
                  <div className="admin-panel-heading"><div><p className="eyebrow">Routing</p><h3>Domains</h3></div><span>{domains.data?.length ?? 0}</span></div>
                  <ul className="domain-list">
                    {domains.data?.map((domain) => <li key={domain.id}><span className={`domain-status ${domain.verified ? "verified" : ""}`} /><strong>{domain.domain}</strong><small>{domain.verified ? "Verified" : "Pending"}</small></li>)}
                    {!domains.isPending && !domains.data?.length && <li className="admin-empty">No domains configured.</li>}
                  </ul>
                  <div className="capacity-bar"><div><span>Attachment storage</span><small>{formatBytes(attachments.data?.total_bytes)} of {formatBytes(attachments.data?.max_bytes)}</small></div><progress value={attachments.data?.total_bytes ?? 0} max={attachments.data?.max_bytes || 1} /></div>
                </article>
              </section>

              <section className="lifecycle-panel">
                <div><p className="eyebrow">Lifecycle</p><h3>Tenant status</h3><p>Lifecycle changes are audited and immediately affect tenant access.</p></div>
                {pendingStatus ? (
                  <div className="transition-confirm" role="alertdialog" aria-label="Confirm tenant status change">
                    <p>Change <strong>{tenant.name}</strong> to <strong>{pendingStatus}</strong>?</p>
                    <button type="button" onClick={() => setPendingStatus(null)}>Cancel</button>
                    <button type="button" className="transition-primary" disabled={transition.isPending} onClick={() => transition.mutate(pendingStatus)}>{transition.isPending ? "Updating…" : "Confirm change"}</button>
                  </div>
                ) : (
                  <div className="lifecycle-actions">
                    {tenant.status !== "active" && <button type="button" onClick={() => setPendingStatus("active")}>Activate</button>}
                    {tenant.status !== "suspended" && tenant.status !== "archived" && <button type="button" onClick={() => setPendingStatus("suspended")}>Suspend</button>}
                    {tenant.status !== "archived" && <button type="button" className="archive" onClick={() => setPendingStatus("archived")}>Archive</button>}
                  </div>
                )}
                {transition.isError && <p className="transition-error" role="alert">The lifecycle change failed. No status was changed.</p>}
              </section>
            </>
          ) : (
            <div className="admin-empty-state"><h2>Select a tenant</h2><p>Choose an environment to inspect operational details.</p></div>
          )}
        </div>
      </div>
    </section>
  );
}

function StatusPill({ status }: { status: TenantStatus }) { return <span className={`tenant-status ${status}`}>{status}</span>; }
function Metric({ label, value, detail }: { label: string; value?: number; detail: string }) { return <article><span>{label}</span><strong>{value?.toLocaleString() ?? "—"}</strong><small>{detail}</small></article>; }
function activeUsers(users: Array<{ status: string }> = []) { return users.filter((user) => user.status === "active").length; }
function initials(value: string) { return value.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "T"; }
function formatBytes(value?: number) { if (value === undefined) return "—"; if (value < 1024) return `${String(value)} B`; if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`; return `${(value / 1024 ** 2).toFixed(1)} MB`; }
