import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  AdminTenant,
  AdminTenantInput,
  AdminTenantListResponse,
  AdminTenantPatch,
  AdminTenantUser,
  AdminTenantUserInput,
  AdminTenantUserPatch,
  TenantStatus,
  TenantUserRole,
  TenantUserStatus,
} from "../api/client";
import { useAuth } from "../auth/auth-context";
import { CredentialSetupDialog } from "../components/CredentialSetupDialog";
import { EntitlementsPanel } from "../components/EntitlementsPanel";
import { ExecutionConfigPanel } from "../components/ExecutionConfigPanel";
import { OAuthImportPanel } from "../components/OAuthImportPanel";
import { TenantOperationsPanel } from "../components/TenantOperationsPanel";

type PendingRemoval =
  | { kind: "user"; id: string; label: string }
  | { kind: "domain"; id: string; label: string };

type DomainAction =
  | { kind: "add"; domain: string }
  | { kind: "verify"; domainId: string }
  | { kind: "delete"; domainId: string };

export function AdminPage({ tenantId: scopedTenantId }: { tenantId?: string }) {
  const tenantScoped = scopedTenantId !== undefined;
  const { api, authentication, session } = useAuth();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [selection, setSelection] = useState<string | null>(null);
  const [tenantEditor, setTenantEditor] = useState<"create" | "edit" | null>(null);
  const [userEditor, setUserEditor] = useState<"create" | AdminTenantUser | null>(null);
  const [credentialUser, setCredentialUser] = useState<AdminTenantUser | null>(null);
  const [pendingRemoval, setPendingRemoval] = useState<PendingRemoval | null>(null);
  const [pendingStatus, setPendingStatus] = useState<"active" | "suspended" | "archived" | null>(null);
  const [domainDraft, setDomainDraft] = useState("");

  const tenants = useQuery({
    queryKey: ["admin-tenants", authentication],
    queryFn: ({ signal }) => api.listAdminTenants(signal),
    enabled: !tenantScoped,
    retry: false,
  });
  const scopedTenant = useQuery({
    queryKey: ["admin-tenant", scopedTenantId, authentication],
    queryFn: ({ signal }) => api.getAdminTenant(scopedTenantId!, signal),
    enabled: tenantScoped,
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
  const tenantId = scopedTenantId ?? selection ?? filteredTenants[0]?.id ?? null;
  const tenant = tenantScoped
    ? scopedTenant.data ?? null
    : tenants.data?.tenants.find((item) => item.id === tenantId) ?? null;

  const users = useQuery({
    queryKey: ["admin-tenant-users", tenantId, authentication],
    queryFn: ({ signal }) => api.listAdminTenantUsers(tenantId, signal),
    enabled: tenantId !== null,
    retry: false,
  });
  const visibleUsers = users.data?.users.filter((user) => user.status !== "deleted") ?? [];
  const domains = useQuery({
    queryKey: ["admin-tenant-domains", tenantId, authentication],
    queryFn: ({ signal }) => api.listAdminTenantDomains(tenantId, signal),
    enabled: tenantId !== null,
    retry: false,
  });
  const attachments = useQuery({
    queryKey: ["admin-attachment-statistics", tenantId, authentication],
    queryFn: ({ signal }) => api.getAdminAttachmentStatistics(tenantId, signal),
    enabled: tenantId !== null && !tenantScoped,
    retry: false,
  });
  const concurrency = useQuery({
    queryKey: ["admin-run-concurrency", tenantId, authentication],
    queryFn: ({ signal }) => api.getAdminRunConcurrency(tenantId, signal),
    enabled: tenantId !== null && !tenantScoped,
    retry: false,
  });

  const tenantSave = useMutation({
    mutationFn: (request: { mode: "create"; input: AdminTenantInput } | { mode: "edit"; input: AdminTenantPatch }) =>
      request.mode === "create"
        ? api.createAdminTenant(request.input)
        : api.updateAdminTenant(tenantId, request.input),
    onSuccess: (saved, request) => {
      setSelection(saved.id);
      setTenantEditor(null);
      if (tenantScoped) {
        queryClient.setQueryData(["admin-tenant", scopedTenantId, authentication], saved);
      } else {
        queryClient.setQueryData<AdminTenantListResponse>(["admin-tenants", authentication], (current) => {
          if (!current) return current;
          const existing = current.tenants.some((item) => item.id === saved.id);
          return {
            ...current,
            tenants: existing
              ? current.tenants.map((item) => item.id === saved.id ? saved : item)
              : [saved, ...current.tenants],
            total: current.total + (request.mode === "create" && !existing ? 1 : 0),
          };
        });
        void queryClient.invalidateQueries({ queryKey: ["admin-tenants"] });
      }
    },
  });
  const userSave = useMutation({
    mutationFn: (request: { mode: "create"; input: AdminTenantUserInput } | { mode: "edit"; userId: string; input: AdminTenantUserPatch }) =>
      request.mode === "create"
        ? api.createAdminTenantUser(tenantId, request.input)
        : api.updateAdminTenantUser(tenantId, request.userId, request.input),
    onSuccess: () => {
      setUserEditor(null);
      void queryClient.invalidateQueries({ queryKey: ["admin-tenant-users", tenantId] });
    },
  });
  const userDelete = useMutation({
    mutationFn: (userId: string) => api.deleteAdminTenantUser(tenantId, userId),
    onSuccess: () => {
      setPendingRemoval(null);
      void queryClient.invalidateQueries({ queryKey: ["admin-tenant-users", tenantId] });
    },
  });
  const domainChange = useMutation({
    mutationFn: async (action: DomainAction) => {
      if (action.kind === "add") await api.addAdminTenantDomain(tenantId, action.domain);
      else if (action.kind === "verify") await api.verifyAdminTenantDomain(tenantId, action.domainId);
      else await api.deleteAdminTenantDomain(tenantId, action.domainId);
    },
    onSuccess: (_, action) => {
      if (action.kind === "add") setDomainDraft("");
      if (action.kind === "delete") setPendingRemoval(null);
      void queryClient.invalidateQueries({ queryKey: ["admin-tenant-domains", tenantId] });
    },
  });
  const transition = useMutation({
    mutationFn: (status: "active" | "suspended" | "archived") =>
      api.transitionAdminTenant(tenantId, status),
    onSuccess: () => {
      setPendingStatus(null);
      void queryClient.invalidateQueries({ queryKey: ["admin-tenants"] });
    },
  });

  function selectTenant(id: string) {
    setSelection(id);
    setPendingStatus(null);
    setUserEditor(null);
    setPendingRemoval(null);
  }

  return (
    <section className="admin-page">
      <header className="admin-heading">
        <div><p className="eyebrow">{tenantScoped ? "Tenant settings" : "Administration"}</p><h1>{tenantScoped ? "Configure your tenant" : "Tenant operations"}</h1><p>{tenantScoped ? "Manage your organization, members, sign-in, domains, and agent runtime." : "Provision customer environments, membership, domains, capacity, and lifecycle state."}</p></div>
        {!tenantScoped && <div className="admin-heading-actions">
          <div className="tenant-total"><strong>{tenants.data?.total ?? "—"}</strong><span>Total tenants</span></div>
          <button type="button" className="admin-primary-action" onClick={() => setTenantEditor("create")}>New tenant</button>
        </div>}
      </header>

      {(tenantScoped ? scopedTenant.isError : tenants.isError) && (
        <div className="admin-access-error" role="alert">
          <strong>{tenantScoped ? "Tenant settings are unavailable." : "Tenant administration is unavailable."}</strong>
          <span>{tenantScoped ? "Confirm that your membership is an active tenant owner, then retry." : "Confirm the admin principal and configured administration store, then retry."}</span>
        </div>
      )}

      <div className={`admin-layout ${tenantScoped ? "tenant-settings-layout" : ""}`}>
        {!tenantScoped && <aside className="tenant-directory" aria-label="Tenant directory">
          <label className="tenant-search"><span className="sr-only">Search tenants</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search tenants…" /></label>
          <div className="tenant-directory-list">
            {filteredTenants.map((item) => (
              <button aria-label={item.name} key={item.id} type="button" className={item.id === tenantId ? "active" : ""} onClick={() => selectTenant(item.id)}>
                <span className="tenant-avatar">{initials(item.name)}</span>
                <span><strong>{item.name}</strong><small>{item.slug}</small></span>
                <StatusPill status={item.status} />
              </button>
            ))}
            {!tenants.isPending && filteredTenants.length === 0 && <p>No matching tenants.</p>}
          </div>
        </aside>}

        <div className="tenant-detail">
          {tenant ? (
            <>
              <section className="tenant-summary">
                <div><span className="tenant-avatar large">{initials(tenant.name)}</span><div><p className="eyebrow">{tenant.id}</p><h2>{tenant.name}</h2><p>{tenant.plan || "No plan"} · {tenant.region || "No region"}</p></div></div>
                <div className="tenant-summary-actions"><StatusPill status={tenant.status} /><button type="button" onClick={() => setTenantEditor("edit")}>Edit tenant</button></div>
              </section>

              <section className="admin-metrics" aria-label="Tenant metrics">
                <Metric label="Members" value={users.data ? visibleUsers.length : undefined} detail={`${activeUsers(visibleUsers)} active`} />
                {!tenantScoped && <Metric label="Active runs" value={concurrency.data?.active_runs} detail={`${concurrency.data?.tenant_capacity ?? "—"} tenant capacity`} />}
                {!tenantScoped && <Metric label="Attachments" value={attachments.data?.total_count} detail={formatBytes(attachments.data?.total_bytes)} />}
                <Metric label="Domains" value={domains.data?.length} detail={`${domains.data?.filter((domain) => domain.verified).length ?? 0} verified`} />
              </section>

              <section className="admin-detail-grid">
                <article className="admin-panel">
                  <div className="admin-panel-heading"><div><p className="eyebrow">Membership</p><h3>Users</h3></div><button type="button" onClick={() => setUserEditor("create")}>Add user</button></div>
                  {users.isError && <p className="inline-error" role="alert">Could not load tenant users.</p>}
                  <ul className="admin-user-list">
                    {visibleUsers.map((user) => <li key={user.id}><span className="user-avatar">{initials(user.display_name || user.user_id)}</span><div><strong>{user.display_name || user.user_id}</strong><small>{user.email || user.user_id} · {user.status}</small></div><span>{user.role}</span><button type="button" aria-label={`Configure sign-in for ${user.display_name || user.user_id}`} onClick={() => setCredentialUser(user)}>Sign-in</button><button type="button" aria-label={`Edit ${user.display_name || user.user_id}`} onClick={() => setUserEditor(user)}>Edit</button><button type="button" className="danger-link" aria-label={`Remove ${user.display_name || user.user_id}`} onClick={() => setPendingRemoval({ kind: "user", id: user.id, label: user.display_name || user.user_id })}>Remove</button></li>)}
                    {!users.isPending && visibleUsers.length === 0 && <li className="admin-empty">No users registered.</li>}
                  </ul>
                </article>
                <article className="admin-panel">
                  <div className="admin-panel-heading"><div><p className="eyebrow">Routing</p><h3>Domains</h3></div><span>{domains.data?.length ?? 0}</span></div>
                  <form className="domain-add-form" onSubmit={(event) => { event.preventDefault(); if (domainDraft.trim()) domainChange.mutate({ kind: "add", domain: domainDraft.trim() }); }}>
                    <label><span className="sr-only">Domain name</span><input required value={domainDraft} onChange={(event) => setDomainDraft(event.target.value)} placeholder="company.example" /></label>
                    <button type="submit" disabled={domainChange.isPending || !domainDraft.trim()}>Add</button>
                  </form>
                  {domains.isError && <p className="inline-error" role="alert">Could not load tenant domains.</p>}
                  {domainChange.isError && <p className="inline-error" role="alert">{mutationError(domainChange.error)}</p>}
                  <ul className="domain-list">
                    {domains.data?.map((domain) => <li key={domain.id}><span className={`domain-status ${domain.verified ? "verified" : ""}`} /><strong>{domain.domain}</strong><small>{domain.verified ? "Verified" : "Pending platform verification"}</small>{!tenantScoped && !domain.verified && <button type="button" onClick={() => domainChange.mutate({ kind: "verify", domainId: domain.id })}>Verify</button>}<button type="button" className="danger-link" aria-label={`Remove ${domain.domain}`} onClick={() => setPendingRemoval({ kind: "domain", id: domain.id, label: domain.domain })}>Remove</button></li>)}
                    {!domains.isPending && !domains.data?.length && <li className="admin-empty">No domains configured.</li>}
                  </ul>
                  {!tenantScoped && <div className="capacity-bar"><div><span>Attachment storage</span><small>{formatBytes(attachments.data?.total_bytes)} of {formatBytes(attachments.data?.max_bytes)}</small></div><progress value={attachments.data?.total_bytes ?? 0} max={attachments.data?.max_bytes || 1} /></div>}
                </article>
              </section>

              <EntitlementsPanel key={`entitlements-${tenant.id}`} tenantId={tenant.id} readOnly={tenantScoped} />
              <ExecutionConfigPanel key={`execution-${tenant.id}`} tenantId={tenant.id} />
              {tenantScoped && <OAuthImportPanel key={`oauth-${tenant.id}`} tenantId={tenant.id} />}
              {!tenantScoped && <TenantOperationsPanel key={`operations-${tenant.id}`} tenantId={tenant.id} />}

              {!tenantScoped && <section className="lifecycle-panel">
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
                {transition.isError && <p className="transition-error" role="alert">{mutationError(transition.error)}</p>}
              </section>}
            </>
          ) : (
            <div className="admin-empty-state"><h2>{tenantScoped ? "Loading tenant settings" : "Select a tenant"}</h2><p>{tenantScoped ? "Retrieving your tenant configuration…" : "Choose an environment to inspect operational details."}</p>{!tenantScoped && <button type="button" className="admin-primary-action" onClick={() => setTenantEditor("create")}>Create the first tenant</button>}</div>
          )}
        </div>
      </div>

      {tenantEditor && <TenantEditor key={`${tenantEditor}-${tenant?.id ?? "new"}`} tenant={tenantEditor === "edit" ? tenant : null} tenantScoped={tenantScoped} defaultId={tenants.data?.total === 0 ? session.principal?.tenant_id : undefined} pending={tenantSave.isPending} error={tenantSave.isError ? mutationError(tenantSave.error) : null} onClose={() => setTenantEditor(null)} onSave={(input) => tenantSave.mutate(tenantEditor === "create" ? { mode: "create", input } : { mode: "edit", input })} />}
      {userEditor && tenant && <UserEditor key={userEditor === "create" ? "new-user" : userEditor.id} user={userEditor === "create" ? null : userEditor} defaultUserId={visibleUsers.length === 0 ? session.principal?.user_id : undefined} pending={userSave.isPending} error={userSave.isError ? mutationError(userSave.error) : null} onClose={() => setUserEditor(null)} onSave={(input) => userSave.mutate(userEditor === "create" ? { mode: "create", input } : { mode: "edit", userId: userEditor.id, input })} />}
      {credentialUser && tenant && <CredentialSetupDialog tenantId={tenant.id} user={credentialUser} onClose={() => setCredentialUser(null)} />}
      {pendingRemoval && <ConfirmationDialog title={pendingRemoval.kind === "user" ? "Remove tenant user?" : "Remove tenant domain?"} message={pendingRemoval.kind === "user" ? `${pendingRemoval.label} will immediately lose access to this tenant.` : `${pendingRemoval.label} will no longer route users to this tenant.`} pending={userDelete.isPending || domainChange.isPending} error={(userDelete.isError && mutationError(userDelete.error)) || (domainChange.isError && mutationError(domainChange.error)) || null} onCancel={() => setPendingRemoval(null)} onConfirm={() => pendingRemoval.kind === "user" ? userDelete.mutate(pendingRemoval.id) : domainChange.mutate({ kind: "delete", domainId: pendingRemoval.id })} />}
    </section>
  );
}

function TenantEditor({ tenant, tenantScoped, defaultId, pending, error, onClose, onSave }: { tenant: AdminTenant | null; tenantScoped: boolean; defaultId?: string; pending: boolean; error: string | null; onClose: () => void; onSave: (input: AdminTenantInput) => void }) {
  const dialogRef = useModalDialog();
  const [id, setId] = useState(defaultId ?? "");
  const [slug, setSlug] = useState(tenant?.slug ?? "");
  const [name, setName] = useState(tenant?.name ?? "");
  const [plan, setPlan] = useState(tenant?.plan ?? "");
  const [region, setRegion] = useState(tenant?.region ?? "");
  const [status, setStatus] = useState<TenantStatus>("provisioning");
  function submit(event: FormEvent) {
    event.preventDefault();
    const common = tenantScoped
      ? { slug: slug.trim(), name: name.trim() }
      : { slug: slug.trim(), name: name.trim(), plan: optional(plan), region: optional(region) };
    onSave(tenant ? common : { ...common, ...(id.trim() ? { id: id.trim() } : {}), status });
  }
  return <dialog ref={dialogRef} className="admin-dialog" aria-labelledby="tenant-editor-title" onCancel={onClose} onClose={onClose}><form onSubmit={submit}><DialogHeading id="tenant-editor-title" title={tenant ? "Edit tenant" : "Create tenant"} onClose={onClose} /><div className="admin-form-grid">{!tenant && <label>Tenant ID <input value={id} onChange={(event) => setId(event.target.value)} placeholder="Generated when blank" /></label>}<label>Slug <input required pattern="[a-z0-9](?:[a-z0-9-]*[a-z0-9])?" value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="acme-corp" /></label><label className="wide">Name <input required value={name} onChange={(event) => setName(event.target.value)} /></label>{!tenantScoped && <><label>Plan <input value={plan} onChange={(event) => setPlan(event.target.value)} placeholder="enterprise" /></label><label>Region <input value={region} onChange={(event) => setRegion(event.target.value)} placeholder="us-east" /></label>{!tenant && <label>Status <select value={status} onChange={(event) => setStatus(event.target.value as TenantStatus)}><option value="provisioning">Provisioning</option><option value="active">Active</option></select></label>}</>}</div>{error && <p className="dialog-error" role="alert">{error}</p>}<DialogActions pending={pending} submitLabel={tenant ? "Save tenant" : "Create tenant"} onClose={onClose} /></form></dialog>;
}

function UserEditor({ user, defaultUserId, pending, error, onClose, onSave }: { user: AdminTenantUser | null; defaultUserId?: string; pending: boolean; error: string | null; onClose: () => void; onSave: (input: AdminTenantUserInput) => void }) {
  const dialogRef = useModalDialog();
  const [userId, setUserId] = useState(user?.user_id ?? defaultUserId ?? "");
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [role, setRole] = useState<TenantUserRole>(user?.role ?? (defaultUserId ? "owner" : "member"));
  const [status, setStatus] = useState<TenantUserStatus>(user?.status === "deleted" ? "suspended" : user?.status ?? (defaultUserId ? "active" : "invited"));
  function submit(event: FormEvent) {
    event.preventDefault();
    const common = { display_name: optional(displayName), email: optional(email), role, status };
    onSave({ ...common, user_id: userId.trim() });
  }
  return <dialog ref={dialogRef} className="admin-dialog" aria-labelledby="user-editor-title" onCancel={onClose} onClose={onClose}><form onSubmit={submit}><DialogHeading id="user-editor-title" title={user ? "Edit tenant user" : "Add tenant user"} onClose={onClose} /><div className="admin-form-grid"><label>User ID <input required disabled={Boolean(user)} value={userId} onChange={(event) => setUserId(event.target.value)} /></label><label>Display name <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label><label className="wide">Email <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></label><label>Role <select value={role} onChange={(event) => setRole(event.target.value as TenantUserRole)}><option value="owner">Owner</option><option value="admin">Admin</option><option value="member">Member</option><option value="viewer">Viewer</option></select></label><label>Status <select value={status} onChange={(event) => setStatus(event.target.value as TenantUserStatus)}><option value="invited">Invited</option><option value="active">Active</option><option value="suspended">Suspended</option></select></label></div>{error && <p className="dialog-error" role="alert">{error}</p>}<DialogActions pending={pending} submitLabel={user ? "Save user" : "Add user"} onClose={onClose} /></form></dialog>;
}

function ConfirmationDialog({ title, message, pending, error, onCancel, onConfirm }: { title: string; message: string; pending: boolean; error: string | null; onCancel: () => void; onConfirm: () => void }) {
  const dialogRef = useModalDialog();
  return <dialog ref={dialogRef} className="admin-dialog admin-confirm-dialog" aria-labelledby="removal-title" onCancel={onCancel} onClose={onCancel}><div><p className="eyebrow">Confirm removal</p><h2 id="removal-title">{title}</h2><p>{message}</p>{error && <p className="dialog-error" role="alert">{error}</p>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onCancel}>Cancel</button><button type="button" className="button button-danger" disabled={pending} onClick={onConfirm}>{pending ? "Removing…" : "Remove"}</button></div></div></dialog>;
}

function useModalDialog() {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);
  return dialogRef;
}

function DialogHeading({ id, title, onClose }: { id: string; title: string; onClose: () => void }) { return <header className="dialog-heading"><div><p className="eyebrow">Tenant administration</p><h2 id={id}>{title}</h2></div><button type="button" className="icon-button" aria-label="Close" onClick={onClose}>×</button></header>; }
function DialogActions({ pending, submitLabel, onClose }: { pending: boolean; submitLabel: string; onClose: () => void }) { return <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onClose}>Cancel</button><button type="submit" className="button button-primary" disabled={pending}>{pending ? "Saving…" : submitLabel}</button></div>; }
function StatusPill({ status }: { status: TenantStatus }) { return <span className={`tenant-status ${status}`}>{status}</span>; }
function Metric({ label, value, detail }: { label: string; value?: number; detail: string }) { return <article><span>{label}</span><strong>{value?.toLocaleString() ?? "—"}</strong><small>{detail}</small></article>; }
function activeUsers(users: Array<{ status: string }> = []) { return users.filter((user) => user.status === "active").length; }
function initials(value: string) { return value.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "T"; }
function formatBytes(value?: number) { if (value === undefined) return "—"; if (value < 1024) return `${String(value)} B`; if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`; return `${(value / 1024 ** 2).toFixed(1)} MB`; }
function optional(value: string) { return value.trim() || null; }
function mutationError(error: unknown) { return error instanceof Error ? error.message : "The request failed. No changes were applied."; }
