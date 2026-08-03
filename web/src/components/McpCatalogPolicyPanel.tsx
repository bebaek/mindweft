import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, type AdminMcpServerCatalogPolicyInput } from "../api/client";
import { useAuth } from "../auth/auth-context";

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
      await queryClient.invalidateQueries({
        queryKey: ["admin-mcp-server-catalog", tenantId],
      });
    },
  });
  const reset = useMutation({
    mutationFn: () => api.deleteAdminMcpServerCatalogPolicy(tenantId),
    onSuccess: async () => {
      setItemIds(null);
      setAllowCustom(null);
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
          <p>Assign deployment-managed MCP services and control whether custom servers are allowed.</p>
        </div>
        {policy.data && <span>Version {policy.data.version}</span>}
      </div>

      {(policy.isPending || catalog.isPending) && <p>Loading catalog policy…</p>}
      {policy.isError && !missing && <p className="inline-error" role="alert">{message(policy.error)}</p>}
      {catalog.isError && <p className="inline-error" role="alert">{message(catalog.error)}</p>}

      {catalog.data && (
        <form onSubmit={submit} className="execution-editor-section">
          {missing && (
            <p className="execution-config-empty">
              Legacy access is active: all deployment services and custom MCP servers are available until this policy is saved.
            </p>
          )}
          <div className="mcp-server-preset-list">
            {catalog.data.items.map((item) => (
              <label className="mcp-server-preset" key={item.id}>
                <input
                  type="checkbox"
                  checked={effectiveItemIds.includes(item.id)}
                  onChange={(event) => toggle(item.id, event.target.checked)}
                />
                <span><strong>{item.title}</strong><small>{item.description}</small></span>
              </label>
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
              {save.isPending ? "Saving…" : "Save catalog assignments"}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed";
}
