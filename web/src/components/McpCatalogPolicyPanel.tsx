import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  type AdminMcpCatalogSubjectType,
  type AdminMcpServerCatalogAccessPreview,
  type AdminMcpServerCatalogItem,
  type AdminMcpServerCatalogPolicyInput,
} from "../api/client";
import { useAuth } from "../auth/auth-context";

const ROLES = ["owner", "admin", "member", "viewer"] as const;

export function McpCatalogPolicyPanel({
  tenantId,
  readOnly = false,
}: {
  tenantId: string;
  readOnly?: boolean;
}) {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const [itemIds, setItemIds] = useState<string[] | null>(null);
  const [allowCustom, setAllowCustom] = useState<boolean | null>(null);
  const [requireAssignment, setRequireAssignment] = useState<boolean | null>(null);
  const policy = useQuery({
    queryKey: ["admin-mcp-catalog-policy", tenantId, authentication],
    queryFn: ({ signal }) => api.getAdminMcpServerCatalogPolicy(tenantId, signal),
    enabled: !readOnly,
    retry: false,
  });
  const catalog = useQuery({
    queryKey: ["admin-deployment-mcp-catalog", authentication],
    queryFn: ({ signal }) => api.getAdminDeploymentMcpServerCatalog(signal),
    enabled: !readOnly,
    retry: false,
  });
  const missing = policy.error instanceof ApiError && policy.error.status === 404;
  const effectiveItemIds = itemIds ?? policy.data?.item_ids ?? (
    missing && catalog.data ? catalog.data.items.map((item) => item.id) : []
  );
  const effectiveAllowCustom = allowCustom ?? policy.data?.allow_custom_mcp_servers ?? missing;
  const effectiveRequireAssignment = (
    requireAssignment ?? policy.data?.require_subject_assignment ?? false
  );
  const preview = useQuery({
    queryKey: [
      "admin-mcp-catalog-access-preview",
      tenantId,
      effectiveRequireAssignment,
      policy.data?.version,
      authentication,
    ],
    queryFn: ({ signal }) => api.previewAdminMcpServerCatalogAccess(
      tenantId, effectiveRequireAssignment, signal,
    ),
    enabled: !readOnly && Boolean(policy.data),
  });

  const save = useMutation({
    mutationFn: (input: AdminMcpServerCatalogPolicyInput) =>
      api.updateAdminMcpServerCatalogPolicy(tenantId, input),
    onSuccess: async (saved) => {
      queryClient.setQueryData(
        ["admin-mcp-catalog-policy", tenantId, authentication],
        saved,
      );
      setItemIds(null);
      setAllowCustom(null);
      setRequireAssignment(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin-mcp-server-catalog", tenantId] }),
        queryClient.invalidateQueries({ queryKey: ["admin-mcp-catalog-assignments", tenantId] }),
        queryClient.invalidateQueries({ queryKey: ["admin-mcp-catalog-access-preview", tenantId] }),
      ]);
    },
  });
  const reset = useMutation({
    mutationFn: () => api.deleteAdminMcpServerCatalogPolicy(tenantId),
    onSuccess: async () => {
      setItemIds(null);
      setAllowCustom(null);
      setRequireAssignment(null);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["admin-mcp-catalog-policy", tenantId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["admin-mcp-server-catalog", tenantId],
        }),
      ]);
    },
  });

  if (readOnly) return null;

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate({
      item_ids: effectiveItemIds,
      allow_custom_mcp_servers: effectiveAllowCustom,
      require_subject_assignment: effectiveRequireAssignment,
    });
  }

  function toggle(itemId: string, checked: boolean) {
    setItemIds(
      checked
        ? [...new Set([...effectiveItemIds, itemId])]
        : effectiveItemIds.filter((id) => id !== itemId),
    );
  }

  return (
    <section className="execution-config-panel" aria-labelledby="mcp-catalog-policy-title">
      <div className="execution-config-heading">
        <div>
          <p className="eyebrow">Tool governance</p>
          <h3 id="mcp-catalog-policy-title">Tenant tool catalog</h3>
          <p>Set the tenant ceiling, then optionally narrow access by role or user.</p>
        </div>
        {policy.data && <span>Version {policy.data.version}</span>}
      </div>

      {(policy.isPending || catalog.isPending) && <p>Loading catalog policy…</p>}
      {policy.isError && !missing && <p className="inline-error" role="alert">{message(policy.error)}</p>}
      {catalog.isError && <p className="inline-error" role="alert">{message(catalog.error)}</p>}

      {catalog.data && (
        <>
          <form onSubmit={submit} className="execution-editor-section">
            {missing && (
              <p className="execution-config-empty">
                Legacy access is active: all deployment services and custom MCP servers are available until this policy is saved.
              </p>
            )}
            <div className="mcp-server-preset-list">
              {catalog.data.items.map((item) => (
                <CatalogCheckbox
                  item={item}
                  checked={effectiveItemIds.includes(item.id)}
                  onChange={(checked) => toggle(item.id, checked)}
                  key={item.id}
                />
              ))}
              {catalog.data.items.length === 0 && <p>No deployment MCP services are configured.</p>}
            </div>
            <label className="execution-checkbox">
              <input
                type="checkbox"
                checked={effectiveAllowCustom}
                onChange={(event) => setAllowCustom(event.target.checked)}
              />
              Allow tenant-defined custom MCP servers
            </label>
            <label className="execution-checkbox">
              <input
                type="checkbox"
                checked={effectiveRequireAssignment}
                onChange={(event) => setRequireAssignment(event.target.checked)}
              />
              Require an explicit user or role assignment (fail closed)
            </label>
            {effectiveRequireAssignment && (
              <p className="execution-config-empty">
                Save a non-empty owner or admin assignment before enabling fail-closed mode.
              </p>
            )}
            {(save.isError || reset.isError) && (
              <p className="inline-error" role="alert">{message(save.error || reset.error)}</p>
            )}
            <div className="dialog-actions">
              {policy.data && (
                <button type="button" className="button button-secondary" disabled={reset.isPending} onClick={() => reset.mutate()}>
                  Restore legacy access
                </button>
              )}
              <button type="submit" className="button button-primary" disabled={save.isPending}>
                {save.isPending ? "Saving…" : "Save tenant ceiling"}
              </button>
            </div>
          </form>
          {policy.data && preview.data && (
            <CatalogAccessPreview preview={preview.data} />
          )}
          {preview.isError && (
            <p className="inline-error" role="alert">{message(preview.error)}</p>
          )}
          {policy.data && (
            <SubjectCatalogAssignments
              tenantId={tenantId}
              catalogItems={catalog.data.items.filter((item) => effectiveItemIds.includes(item.id))}
              requireSubjectAssignment={effectiveRequireAssignment}
            />
          )}
        </>
      )}
    </section>
  );
}

function CatalogAccessPreview({
  preview,
}: {
  preview: AdminMcpServerCatalogAccessPreview;
}) {
  return (
    <div className="execution-editor-section">
      <h4>Effective access preview</h4>
      <p>
        {preview.require_subject_assignment
          ? "Fail-closed assignment enforcement preview."
          : "Current assignment and tenant-inheritance preview."}
      </p>
      {preview.users.map((user) => (
        <div className="mcp-server-preset" key={user.user_id}>
          <span>
            <strong>{user.display_name || user.email || user.user_id}</strong>
            <small>{user.role} · {user.status} · source: {user.source}</small>
          </span>
          <small className={user.denied && user.status === "active" ? "inline-error" : undefined}>
            {user.item_ids.length ? user.item_ids.join(", ") : "No managed MCP services"}
          </small>
        </div>
      ))}
    </div>
  );
}

function SubjectCatalogAssignments({
  tenantId,
  catalogItems,
  requireSubjectAssignment,
}: {
  tenantId: string;
  catalogItems: AdminMcpServerCatalogItem[];
  requireSubjectAssignment: boolean;
}) {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const [subjectKey, setSubjectKey] = useState("role:member");
  const [drafts, setDrafts] = useState<Record<string, string[]>>({});
  const [subjectType, subjectId] = subjectKey.split(":", 2) as [AdminMcpCatalogSubjectType, string];
  const assignments = useQuery({
    queryKey: ["admin-mcp-catalog-assignments", tenantId, authentication],
    queryFn: ({ signal }) => api.listAdminMcpServerCatalogAssignments(tenantId, signal),
  });
  const users = useQuery({
    queryKey: ["admin-tenant-users", tenantId, authentication],
    queryFn: ({ signal }) => api.listAdminTenantUsers(tenantId, signal),
  });
  const assignment = assignments.data?.assignments.find(
    (item) => item.subject_type === subjectType && item.subject_id === subjectId,
  );
  const selectedItemIds = drafts[subjectKey] ?? assignment?.item_ids ?? [];

  const save = useMutation({
    mutationFn: () => api.updateAdminMcpServerCatalogAssignment(
      tenantId, subjectType, subjectId, selectedItemIds,
    ),
    onSuccess: async () => {
      setDrafts((current) => {
        const next = { ...current };
        delete next[subjectKey];
        return next;
      });
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["admin-mcp-catalog-assignments", tenantId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["admin-mcp-catalog-access-preview", tenantId],
        }),
      ]);
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteAdminMcpServerCatalogAssignment(
      tenantId, subjectType, subjectId,
    ),
    onSuccess: async () => {
      setDrafts((current) => {
        const next = { ...current };
        delete next[subjectKey];
        return next;
      });
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["admin-mcp-catalog-assignments", tenantId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["admin-mcp-catalog-access-preview", tenantId],
        }),
      ]);
    },
  });

  function toggle(itemId: string, checked: boolean) {
    setDrafts((current) => ({
      ...current,
      [subjectKey]: checked
        ? [...new Set([...selectedItemIds, itemId])]
        : selectedItemIds.filter((id) => id !== itemId),
    }));
  }

  return (
    <div className="execution-editor-section">
      <div className="execution-config-heading">
        <div>
          <h4>Role and user assignments</h4>
          <p>User assignments take precedence over role assignments. Both remain bounded by the tenant ceiling.</p>
        </div>
      </div>
      <label>
        Subject
        <select value={subjectKey} onChange={(event) => setSubjectKey(event.target.value)}>
          <optgroup label="Roles">
            {ROLES.map((role) => <option value={`role:${role}`} key={role}>Role: {role}</option>)}
          </optgroup>
          <optgroup label="Users">
            {users.data?.users.map((user) => (
              <option value={`user:${user.user_id}`} key={user.id}>
                User: {user.display_name || user.email || user.user_id}
              </option>
            ))}
          </optgroup>
        </select>
      </label>
      {!assignment && drafts[subjectKey] === undefined && (
        <p className="execution-config-empty">
          No assignment: {requireSubjectAssignment
            ? "this subject receives no managed MCP services."
            : "this subject inherits tenant access."}
        </p>
      )}
      <div className="mcp-server-preset-list">
        {catalogItems.map((item) => (
          <CatalogCheckbox
            item={item}
            checked={selectedItemIds.includes(item.id)}
            onChange={(checked) => toggle(item.id, checked)}
            key={item.id}
          />
        ))}
      </div>
      {(assignments.isError || users.isError || save.isError || remove.isError) && (
        <p className="inline-error" role="alert">
          {message(assignments.error || users.error || save.error || remove.error)}
        </p>
      )}
      <div className="dialog-actions">
        {assignment && (
          <button type="button" className="button button-secondary" onClick={() => remove.mutate()} disabled={remove.isPending}>
            Restore inheritance
          </button>
        )}
        <button type="button" className="button button-primary" onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save subject assignment"}
        </button>
      </div>
    </div>
  );
}

function CatalogCheckbox({
  item,
  checked,
  onChange,
}: {
  item: AdminMcpServerCatalogItem;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="mcp-server-preset">
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span><strong>{item.title}</strong><small>{item.description}</small></span>
    </label>
  );
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed";
}
