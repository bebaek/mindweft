import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { AdminUserDeprovisioningEvent } from "../api/client";
import { useAuth } from "../auth/auth-context";

export function UserDeprovisioningPanel({ tenantId }: { tenantId: string }) {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const events = useQuery({
    queryKey: ["admin-user-deprovisioning-events", tenantId, authentication],
    queryFn: ({ signal }) => api.listAdminUserDeprovisioningEvents(tenantId, signal),
    refetchInterval: 5_000,
  });
  const retry = useMutation({
    mutationFn: (eventId: string) => api.retryAdminUserDeprovisioningEvent(tenantId, eventId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["admin-user-deprovisioning-events", tenantId],
      });
    },
  });
  const unresolved = events.data?.events.filter((event) => event.state !== "completed") ?? [];

  return (
    <section className="execution-config-panel" aria-labelledby="user-deprovisioning-title">
      <div className="execution-config-heading">
        <div>
          <p className="eyebrow">Identity lifecycle</p>
          <h3 id="user-deprovisioning-title">User deprovisioning</h3>
          <p>
            Suspended and deleted users are denied immediately. Durable cleanup removes explicit MCP assignments and disables external grants.
          </p>
        </div>
        <span>{unresolved.length} unresolved</span>
      </div>
      {events.isPending && <p>Loading deprovisioning status…</p>}
      {events.isError && <p className="inline-error" role="alert">{message(events.error)}</p>}
      {retry.isError && <p className="inline-error" role="alert">{message(retry.error)}</p>}
      <div className="mcp-server-preset-list">
        {events.data?.events.map((event) => (
          <DeprovisioningEventRow
            event={event}
            retrying={retry.isPending}
            onRetry={() => retry.mutate(event.id)}
            key={event.id}
          />
        ))}
        {events.data?.events.length === 0 && (
          <p className="execution-config-empty">No user deprovisioning events.</p>
        )}
      </div>
    </section>
  );
}

function DeprovisioningEventRow({
  event,
  retrying,
  onRetry,
}: {
  event: AdminUserDeprovisioningEvent;
  retrying: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="mcp-server-preset">
      <span>
        <strong>{event.user_id}</strong>
        <small>{event.target_status} · {stateLabel(event.state)} · attempt {event.attempts}</small>
        <small>
          Assignment removed: {event.assignment_removed ? "yes" : "no"} · grants disabled: {event.grants_disabled}
        </small>
        {event.last_error && <small className="inline-error">{event.last_error}</small>}
      </span>
      <span>
        <small>{new Date(event.updated_at).toLocaleString()}</small>
        {event.state === "dead_letter" && (
          <button type="button" disabled={retrying} onClick={onRetry}>
            {retrying ? "Retrying…" : "Retry"}
          </button>
        )}
      </span>
    </div>
  );
}

function stateLabel(state: AdminUserDeprovisioningEvent["state"]): string {
  return state.replace("_", " ");
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed";
}
