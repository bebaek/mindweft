import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  AdminExternalGrant,
  AdminExternalGrantInput,
  AdminExternalGrantProvider,
  AdminExternalGrantResource,
} from "../api/client";
import { useAuth } from "../auth/auth-context";

export function ExternalGrantPanel({ tenantId, readOnly = false }: { tenantId: string; readOnly?: boolean }) {
  const { api, authentication } = useAuth();
  const [providerId, setProviderId] = useState<string | null>(null);
  const [auditFilter, setAuditFilter] = useState("");
  const [auditLimit, setAuditLimit] = useState(25);
  const providers = useQuery({
    queryKey: ["admin-external-grant-providers", authentication],
    queryFn: ({ signal }) => api.listAdminExternalGrantProviders(signal),
    enabled: !readOnly,
  });
  const selectedProvider = providers.data?.providers.find(
    (provider) => provider.id === providerId,
  ) ?? providers.data?.providers[0];
  const grants = useQuery({
    queryKey: ["admin-external-grants", tenantId, selectedProvider?.id, authentication],
    queryFn: ({ signal }) => api.listAdminExternalGrants(tenantId, selectedProvider!.id, signal),
    enabled: !readOnly && Boolean(selectedProvider),
  });

  const resources = useQuery({
    queryKey: ["admin-external-grant-resources", tenantId, selectedProvider?.id, authentication],
    queryFn: ({ signal }) => api.listAdminExternalGrantResources(
      tenantId, selectedProvider!.id, signal,
    ),
    enabled: !readOnly && Boolean(selectedProvider?.resource_discovery_available),
  });
  const resourceById = new Map(
    resources.data?.resources.map((resource) => [resource.resource_id, resource]) ?? [],
  );

  const audit = useQuery({
    queryKey: [
      "admin-external-grant-audit",
      tenantId,
      selectedProvider?.id,
      authentication,
      auditLimit,
    ],
    queryFn: ({ signal }) => api.listAdminExternalGrantAudit(
      tenantId, selectedProvider!.id, { limit: auditLimit }, signal,
    ),
    enabled: !readOnly && Boolean(selectedProvider?.audit_available),
  });
  const normalizedAuditFilter = auditFilter.trim().toLocaleLowerCase();
  const visibleAuditEntries = audit.data?.entries.filter((entry) => {
    if (!normalizedAuditFilter) return true;
    const resource = resourceById.get(entry.resource_id);
    return [
      entry.resource_id,
      resource?.label,
      entry.subject_id,
      entry.actor_id,
      entry.operation,
    ].some((value) => value?.toLocaleLowerCase().includes(normalizedAuditFilter));
  });

  if (readOnly) return null;
  if (providers.isPending) return <p>Loading external grant providers…</p>;
  if (providers.isError) return <p className="inline-error" role="alert">{message(providers.error)}</p>;
  if (!selectedProvider) return null;

  return (
    <section className="execution-config-panel" aria-labelledby="external-grants-title">
      <div className="execution-config-heading">
        <div>
          <p className="eyebrow">External authorization</p>
          <h3 id="external-grants-title">External resource grants</h3>
          <p>
            Manage grants at the authoritative provider. These permissions are never exposed as model tools.
          </p>
        </div>
      </div>
      {providers.data.providers.length > 1 && (
        <label>
          Provider
          <select
            value={selectedProvider.id}
            onChange={(event) => setProviderId(event.target.value)}
          >
            {providers.data.providers.map((provider) => (
              <option value={provider.id} key={provider.id}>{provider.title}</option>
            ))}
          </select>
        </label>
      )}
      <p>{selectedProvider.description}</p>
      {grants.isPending && <p>Loading grants…</p>}
      {grants.isError && <p className="inline-error" role="alert">{message(grants.error)}</p>}
      {resources.isError && (
        <p className="inline-error" role="alert">{message(resources.error)}</p>
      )}
      {grants.data && (
        <div className="execution-editor-section">
          <div className="mcp-server-preset-list">
            {grants.data.grants.map((grant) => (
              <GrantRow
                tenantId={tenantId}
                provider={selectedProvider}
                resource={resourceById.get(grant.resource_id)}
                grant={grant}
                key={`${grant.resource_id}:${grant.subject_id}:${grant.permission}:${grant.enabled}`}
              />
            ))}
            {grants.data.grants.length === 0 && (
              <p className="execution-config-empty">No grants are configured for this tenant.</p>
            )}
          </div>
          {selectedProvider.resource_discovery_available && resources.isPending && (
            <p>Loading resource catalog…</p>
          )}
          {(!selectedProvider.resource_discovery_available || resources.data) && (
            <NewGrantForm
              tenantId={tenantId}
              provider={selectedProvider}
              resources={resources.data?.resources}
            />
          )}
          {selectedProvider.audit_available && (
            <div className="execution-editor-section">
              <h4>Provider audit history</h4>
              <p>Immutable grant mutations reported by the authoritative provider.</p>
              <label>
                Filter audit history
                <input
                  type="search"
                  value={auditFilter}
                  placeholder="Resource, subject, actor, or operation"
                  onChange={(event) => setAuditFilter(event.target.value)}
                />
              </label>
              {audit.isPending && <p>Loading provider audit…</p>}
              {audit.isError && <p className="inline-error" role="alert">{message(audit.error)}</p>}
              {visibleAuditEntries?.map((entry) => (
                <div className="mcp-server-preset" key={entry.audit_id}>
                  <span>
                    <strong>{operationLabel(entry.operation)}</strong>
                    <small>
                      {resourceLabel(resourceById.get(entry.resource_id), entry.resource_id)} · {entry.subject_id}
                    </small>
                    <small>Actor: {entry.actor_id} · {new Date(entry.created_at).toLocaleString()}</small>
                  </span>
                  <span>
                    <small>Before: {grantStateLabel(entry.previous)}</small>
                    <small>After: {grantStateLabel(entry.resulting)}</small>
                  </span>
                </div>
              ))}
              {audit.data && visibleAuditEntries?.length === 0 && (
                <p className="execution-config-empty">
                  {audit.data.entries.length === 0
                    ? "No provider audit records are available."
                    : "No audit records match this filter."}
                </p>
              )}
              {audit.data?.next_cursor && auditLimit < 500 && (
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() => setAuditLimit((value) => Math.min(value + 25, 500))}
                >
                  Load older
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function GrantRow({
  tenantId,
  provider,
  resource,
  grant,
}: {
  tenantId: string;
  provider: AdminExternalGrantProvider;
  resource?: AdminExternalGrantResource;
  grant: AdminExternalGrant;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [permission, setPermission] = useState(grant.permission);
  const [enabled, setEnabled] = useState(grant.enabled);
  const permissions = resource?.allowed_permissions ?? provider.allowed_permissions;
  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["admin-external-grants", tenantId, provider.id],
      }),
      queryClient.invalidateQueries({
        queryKey: ["admin-external-grant-audit", tenantId, provider.id],
      }),
    ]);
  };
  const save = useMutation({
    mutationFn: (input: AdminExternalGrantInput) =>
      api.updateAdminExternalGrant(tenantId, provider.id, input),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteAdminExternalGrant(
      tenantId, provider.id, grant.resource_id, grant.subject_id,
    ),
    onSuccess: invalidate,
  });

  return (
    <div className="mcp-server-preset">
      <span>
        <strong>{resource?.label ?? grant.resource_id}</strong>
        {resource && <small>{grant.resource_id} · {resource.kind.toUpperCase()}</small>}
        <small>Subject: {grant.subject_id}</small>
      </span>
      <label>
        Permission
        <select value={permission} onChange={(event) => setPermission(event.target.value)}>
          {permissions.map((item) => <option value={item} key={item}>{item}</option>)}
        </select>
      </label>
      <label className="execution-checkbox">
        <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
        Enabled
      </label>
      {(save.isError || remove.isError) && (
        <p className="inline-error" role="alert">{message(save.error || remove.error)}</p>
      )}
      <div className="dialog-actions">
        <button
          type="button"
          className="button button-secondary"
          disabled={remove.isPending}
          onClick={() => {
            if (window.confirm(`Delete grant for ${grant.resource_id}?`)) remove.mutate();
          }}
        >
          Delete
        </button>
        <button
          type="button"
          className="button button-primary"
          disabled={save.isPending}
          onClick={() => save.mutate({
            resource_id: grant.resource_id,
            subject_id: grant.subject_id,
            permission,
            enabled,
          })}
        >
          {save.isPending ? "Saving…" : "Save grant"}
        </button>
      </div>
    </div>
  );
}

function NewGrantForm({
  tenantId,
  provider,
  resources,
}: {
  tenantId: string;
  provider: AdminExternalGrantProvider;
  resources?: AdminExternalGrantResource[];
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [resourceId, setResourceId] = useState("");
  const [subjectId, setSubjectId] = useState("");
  const availableResources = resources?.filter((resource) => resource.configured && resource.enabled);
  const [permission, setPermission] = useState(
    availableResources?.[0]?.allowed_permissions[0] ?? provider.allowed_permissions[0] ?? "read",
  );
  const selectedResource = availableResources?.find(
    (resource) => resource.resource_id === resourceId,
  ) ?? availableResources?.[0];
  const permissions = selectedResource?.allowed_permissions ?? provider.allowed_permissions;
  const create = useMutation({
    mutationFn: (input: AdminExternalGrantInput) =>
      api.updateAdminExternalGrant(tenantId, provider.id, input),
    onSuccess: async () => {
      setResourceId("");
      setSubjectId("");
      setPermission(
        availableResources?.[0]?.allowed_permissions[0] ?? provider.allowed_permissions[0] ?? "read",
      );
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["admin-external-grants", tenantId, provider.id],
        }),
        queryClient.invalidateQueries({
          queryKey: ["admin-external-grant-audit", tenantId, provider.id],
        }),
      ]);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate({
      resource_id: (selectedResource?.resource_id ?? resourceId).trim(),
      subject_id: subjectId.trim(),
      permission,
      enabled: true,
    });
  }

  if (availableResources && availableResources.length === 0) {
    return <p className="execution-config-empty">No enabled provider resources are available.</p>;
  }

  return (
    <form onSubmit={submit} className="execution-editor-section">
      <h4>Add grant</h4>
      <label>
        {availableResources ? "Resource" : "Resource ID"}
        {availableResources ? (
          <select
            required
            value={selectedResource?.resource_id ?? ""}
            onChange={(event) => {
              const next = availableResources.find(
                (resource) => resource.resource_id === event.target.value,
              );
              setResourceId(event.target.value);
              if (next && !next.allowed_permissions.includes(permission)) {
                setPermission(next.allowed_permissions[0] ?? "read");
              }
            }}
          >
            {availableResources.map((resource) => (
              <option value={resource.resource_id} key={resource.resource_id}>
                {resource.label} ({resource.kind.toUpperCase()})
              </option>
            ))}
          </select>
        ) : (
          <input required value={resourceId} onChange={(event) => setResourceId(event.target.value)} />
        )}
      </label>
      <label>
        Subject ID
        <input required value={subjectId} onChange={(event) => setSubjectId(event.target.value)} />
      </label>
      <label>
        Permission
        <select value={permission} onChange={(event) => setPermission(event.target.value)}>
          {permissions.map((item) => <option value={item} key={item}>{item}</option>)}
        </select>
      </label>
      {create.isError && <p className="inline-error" role="alert">{message(create.error)}</p>}
      <div className="dialog-actions">
        <button type="submit" className="button button-primary" disabled={create.isPending}>
          {create.isPending ? "Adding…" : "Add grant"}
        </button>
      </div>
    </form>
  );
}

function resourceLabel(resource: AdminExternalGrantResource | undefined, resourceId: string): string {
  return resource ? `${resource.label} (${resourceId})` : resourceId;
}

function operationLabel(operation: string): string {
  return operation.replace(/^resource_grant\./, "").replaceAll("_", " ");
}

function grantStateLabel(state: { permission: string; enabled: boolean } | null): string {
  if (!state) return "none";
  return `${state.permission}, ${state.enabled ? "enabled" : "disabled"}`;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed";
}
