import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  AdminExternalGrant,
  AdminExternalGrantInput,
  AdminExternalGrantProvider,
} from "../api/client";
import { useAuth } from "../auth/auth-context";

export function ExternalGrantPanel({ tenantId, readOnly = false }: { tenantId: string; readOnly?: boolean }) {
  const { api, authentication } = useAuth();
  const [providerId, setProviderId] = useState<string | null>(null);
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

  const audit = useQuery({
    queryKey: ["admin-external-grant-audit", tenantId, selectedProvider?.id, authentication],
    queryFn: ({ signal }) => api.listAdminExternalGrantAudit(
      tenantId, selectedProvider!.id, { limit: 25 }, signal,
    ),
    enabled: !readOnly && Boolean(selectedProvider?.audit_available),
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
      {grants.data && (
        <div className="execution-editor-section">
          <div className="mcp-server-preset-list">
            {grants.data.grants.map((grant) => (
              <GrantRow
                tenantId={tenantId}
                provider={selectedProvider}
                grant={grant}
                key={`${grant.resource_id}:${grant.subject_id}:${grant.permission}:${grant.enabled}`}
              />
            ))}
            {grants.data.grants.length === 0 && (
              <p className="execution-config-empty">No grants are configured for this tenant.</p>
            )}
          </div>
          <NewGrantForm tenantId={tenantId} provider={selectedProvider} />
          {selectedProvider.audit_available && (
            <div className="execution-editor-section">
              <h4>Provider audit history</h4>
              <p>Immutable grant mutations reported by the authoritative provider.</p>
              {audit.isPending && <p>Loading provider audit…</p>}
              {audit.isError && <p className="inline-error" role="alert">{message(audit.error)}</p>}
              {audit.data?.entries.map((entry) => (
                <div className="mcp-server-preset" key={entry.audit_id}>
                  <span>
                    <strong>{operationLabel(entry.operation)}</strong>
                    <small>{entry.resource_id} · {entry.subject_id}</small>
                    <small>Actor: {entry.actor_id} · {new Date(entry.created_at).toLocaleString()}</small>
                  </span>
                  <span>
                    <small>Before: {grantStateLabel(entry.previous)}</small>
                    <small>After: {grantStateLabel(entry.resulting)}</small>
                  </span>
                </div>
              ))}
              {audit.data?.entries.length === 0 && (
                <p className="execution-config-empty">No provider audit records are available.</p>
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
  grant,
}: {
  tenantId: string;
  provider: AdminExternalGrantProvider;
  grant: AdminExternalGrant;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [permission, setPermission] = useState(grant.permission);
  const [enabled, setEnabled] = useState(grant.enabled);
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
        <strong>{grant.resource_id}</strong>
        <small>Subject: {grant.subject_id}</small>
      </span>
      <label>
        Permission
        <select value={permission} onChange={(event) => setPermission(event.target.value)}>
          {provider.allowed_permissions.map((item) => <option value={item} key={item}>{item}</option>)}
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
}: {
  tenantId: string;
  provider: AdminExternalGrantProvider;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [resourceId, setResourceId] = useState("");
  const [subjectId, setSubjectId] = useState("");
  const [permission, setPermission] = useState(provider.allowed_permissions[0] ?? "read");
  const create = useMutation({
    mutationFn: (input: AdminExternalGrantInput) =>
      api.updateAdminExternalGrant(tenantId, provider.id, input),
    onSuccess: async () => {
      setResourceId("");
      setSubjectId("");
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
      resource_id: resourceId.trim(),
      subject_id: subjectId.trim(),
      permission,
      enabled: true,
    });
  }

  return (
    <form onSubmit={submit} className="execution-editor-section">
      <h4>Add grant</h4>
      <label>
        Resource ID
        <input required value={resourceId} onChange={(event) => setResourceId(event.target.value)} />
      </label>
      <label>
        Subject ID
        <input required value={subjectId} onChange={(event) => setSubjectId(event.target.value)} />
      </label>
      <label>
        Permission
        <select value={permission} onChange={(event) => setPermission(event.target.value)}>
          {provider.allowed_permissions.map((item) => <option value={item} key={item}>{item}</option>)}
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
