import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  type AdminTenantEntitlements,
  type AdminTenantEntitlementsInput,
  type EntitlementLimitValue,
} from "../api/client";
import { useAuth } from "../auth/auth-context";

type FeatureRow = { id: number; key: string; enabled: boolean };
type LimitType = "number" | "text" | "boolean" | "unlimited";
type LimitRow = { id: number; key: string; type: LimitType; value: string };

export function EntitlementsPanel({ tenantId, readOnly = false }: { tenantId: string; readOnly?: boolean }) {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const entitlements = useQuery({
    queryKey: ["admin-tenant-entitlements", tenantId, authentication],
    queryFn: ({ signal }) => api.getAdminTenantEntitlements(tenantId, signal),
    retry: false,
  });
  const missing = entitlements.error instanceof ApiError && entitlements.error.status === 404;
  const save = useMutation({
    mutationFn: async (input: AdminTenantEntitlementsInput) => {
      const report = await api.validateAdminTenantEntitlements(tenantId, input);
      if (!report.valid) {
        throw new EntitlementsValidationError([
          ...report.features.errors,
          ...report.limits.errors,
        ]);
      }
      return api.updateAdminTenantEntitlements(tenantId, input);
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(
        ["admin-tenant-entitlements", tenantId, authentication],
        saved,
      );
      setEditing(false);
    },
  });
  const reset = useMutation({
    mutationFn: () => api.deleteAdminTenantEntitlements(tenantId),
    onSuccess: () => {
      queryClient.removeQueries({
        queryKey: ["admin-tenant-entitlements", tenantId, authentication],
        exact: true,
      });
      setConfirmReset(false);
      void queryClient.invalidateQueries({
        queryKey: ["admin-tenant-entitlements", tenantId],
      });
    },
  });

  function openEditor() {
    save.reset();
    setEditing(true);
  }

  function closeEditor() {
    save.reset();
    setEditing(false);
  }

  function openResetConfirmation() {
    reset.reset();
    setConfirmReset(true);
  }

  return (
    <section className="entitlements-panel" aria-labelledby="entitlements-title">
      <div className="entitlements-heading">
        <div>
          <p className="eyebrow">Access policy</p>
          <h3 id="entitlements-title">Entitlements</h3>
          <p>Control tenant feature availability and plan-specific limits.</p>
        </div>
        <div className="entitlements-heading-actions">
          {entitlements.data && (
            <span title={formatDate(entitlements.data.updated_at)}>
              Version {entitlements.data.version}
            </span>
          )}
          {!readOnly && <button type="button" onClick={openEditor}>
            {entitlements.data ? "Edit entitlements" : "Configure entitlements"}
          </button>}
        </div>
      </div>

      {entitlements.isPending && <p className="entitlements-loading">Loading entitlements…</p>}
      {entitlements.isError && !missing && (
        <p className="inline-error" role="alert">{errorMessage(entitlements.error)}</p>
      )}
      {missing && (
        <div className="entitlements-empty">
          <strong>No tenant-specific entitlements</strong>
          <span>The runtime will use its default feature and limit policy.</span>
        </div>
      )}
      {entitlements.data && (
        <div className="entitlements-content">
          <EntitlementGroup
            title="Feature flags"
            empty="No feature overrides."
            items={Object.entries(entitlements.data.features).map(([key, enabled]) => ({
              key,
              value: enabled ? "Enabled" : "Disabled",
              tone: enabled ? "enabled" : "disabled",
            }))}
          />
          <EntitlementGroup
            title="Plan limits"
            empty="No limit overrides."
            items={Object.entries(entitlements.data.limits).map(([key, value]) => ({
              key,
              value: formatLimit(value),
            }))}
          />
          <footer>
            <span>Updated {formatDate(entitlements.data.updated_at)}</span>
            {!readOnly && <button type="button" onClick={openResetConfirmation}>Reset to defaults</button>}
          </footer>
        </div>
      )}

      {!readOnly && editing && (
        <EntitlementsEditor
          key={`${tenantId}-${entitlements.data?.version ?? "new"}`}
          current={entitlements.data ?? null}
          pending={save.isPending}
          error={save.isError ? errorMessage(save.error) : null}
          onClose={closeEditor}
          onSave={(input) => save.mutate(input)}
        />
      )}
      {!readOnly && confirmReset && (
        <ResetDialog
          pending={reset.isPending}
          error={reset.isError ? errorMessage(reset.error) : null}
          onCancel={() => setConfirmReset(false)}
          onConfirm={() => reset.mutate()}
        />
      )}
    </section>
  );
}

function EntitlementGroup({ title, empty, items }: { title: string; empty: string; items: Array<{ key: string; value: string; tone?: string }> }) {
  return <div className="entitlement-group"><h4>{title}</h4>{items.length ? <dl>{items.map((item) => <div key={item.key}><dt>{item.key}</dt><dd className={item.tone}>{item.value}</dd></div>)}</dl> : <p>{empty}</p>}</div>;
}

function EntitlementsEditor({ current, pending, error, onClose, onSave }: { current: AdminTenantEntitlements | null; pending: boolean; error: string | null; onClose: () => void; onSave: (input: AdminTenantEntitlementsInput) => void }) {
  const dialogRef = useModalDialog();
  const featureEntries = Object.entries(current?.features ?? {});
  const limitEntries = Object.entries(current?.limits ?? {});
  const nextId = useRef(featureEntries.length + limitEntries.length + 1);
  const [features, setFeatures] = useState<FeatureRow[]>(() =>
    featureEntries.map(([key, enabled], index) => ({
      id: index + 1, key, enabled,
    })),
  );
  const [limits, setLimits] = useState<LimitRow[]>(() =>
    limitEntries.map(([key, value], index) => ({
      id: featureEntries.length + index + 1, key, ...limitRowValue(value),
    })),
  );
  const [localError, setLocalError] = useState<string | null>(null);

  function submit(event: FormEvent) {
    event.preventDefault();
    const keys = [...features.map((row) => row.key.trim()), ...limits.map((row) => row.key.trim())];
    if (keys.some((key) => !key)) {
      setLocalError("Every feature and limit needs a name.");
      return;
    }
    if (new Set(features.map((row) => row.key.trim())).size !== features.length) {
      setLocalError("Feature names must be unique.");
      return;
    }
    if (new Set(limits.map((row) => row.key.trim())).size !== limits.length) {
      setLocalError("Limit names must be unique.");
      return;
    }
    const input: AdminTenantEntitlementsInput = { features: {}, limits: {} };
    for (const row of features) input.features[row.key.trim()] = row.enabled;
    for (const row of limits) {
      const value = parseLimit(row);
      if (value === INVALID_LIMIT) {
        setLocalError(`Limit '${row.key.trim()}' needs a valid number.`);
        return;
      }
      input.limits[row.key.trim()] = value;
    }
    setLocalError(null);
    onSave(input);
  }

  return (
    <dialog ref={dialogRef} className="admin-dialog entitlements-dialog" aria-labelledby="entitlements-editor-title" onCancel={onClose} onClose={onClose}>
      <form onSubmit={submit}>
        <header className="dialog-heading"><div><p className="eyebrow">Access policy</p><h2 id="entitlements-editor-title">{current ? "Edit entitlements" : "Configure entitlements"}</h2></div><button type="button" className="icon-button" aria-label="Close" onClick={onClose}>×</button></header>
        <p className="entitlements-editor-copy">Changes are validated by the server before they are applied. Removing a row removes that override.</p>

        <div className="entitlement-editor-section">
          <div><h3>Feature flags</h3><button type="button" onClick={() => setFeatures((rows) => [...rows, { id: nextId.current++, key: "", enabled: true }])}>Add feature</button></div>
          <div className="entitlement-rows">
            {features.map((row) => <div className="entitlement-row feature" key={row.id}><label>Feature name<input required value={row.key} onChange={(event) => setFeatures((rows) => rows.map((item) => item.id === row.id ? { ...item, key: event.target.value } : item))} placeholder="feature_name" /></label><label>State<select value={row.enabled ? "enabled" : "disabled"} onChange={(event) => setFeatures((rows) => rows.map((item) => item.id === row.id ? { ...item, enabled: event.target.value === "enabled" } : item))}><option value="enabled">Enabled</option><option value="disabled">Disabled</option></select></label><button type="button" aria-label={`Remove feature ${row.key || "row"}`} onClick={() => setFeatures((rows) => rows.filter((item) => item.id !== row.id))}>Remove</button></div>)}
            {!features.length && <p>No feature overrides.</p>}
          </div>
        </div>

        <div className="entitlement-editor-section">
          <div><h3>Plan limits</h3><button type="button" onClick={() => setLimits((rows) => [...rows, { id: nextId.current++, key: "", type: "number", value: "" }])}>Add limit</button></div>
          <div className="entitlement-rows">
            {limits.map((row) => <div className="entitlement-row limit" key={row.id}><label>Limit name<input required value={row.key} onChange={(event) => setLimits((rows) => rows.map((item) => item.id === row.id ? { ...item, key: event.target.value } : item))} placeholder="max_threads" /></label><label>Type<select value={row.type} onChange={(event) => setLimits((rows) => rows.map((item) => item.id === row.id ? { ...item, type: event.target.value as LimitType, value: defaultLimitValue(event.target.value as LimitType) } : item))}><option value="number">Number</option><option value="text">Text</option><option value="boolean">Boolean</option><option value="unlimited">Unlimited</option></select></label>{row.type === "boolean" ? <label>Value<select value={row.value} onChange={(event) => setLimits((rows) => rows.map((item) => item.id === row.id ? { ...item, value: event.target.value } : item))}><option value="true">True</option><option value="false">False</option></select></label> : row.type === "unlimited" ? <span className="unlimited-value">No limit</span> : <label>Value<input required value={row.value} type={row.type === "number" ? "number" : "text"} step={row.type === "number" ? "any" : undefined} onChange={(event) => setLimits((rows) => rows.map((item) => item.id === row.id ? { ...item, value: event.target.value } : item))} /></label>}<button type="button" aria-label={`Remove limit ${row.key || "row"}`} onClick={() => setLimits((rows) => rows.filter((item) => item.id !== row.id))}>Remove</button></div>)}
            {!limits.length && <p>No limit overrides.</p>}
          </div>
        </div>

        {(localError || error) && <p className="dialog-error" role="alert">{localError || error}</p>}
        <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onClose}>Cancel</button><button type="submit" className="button button-primary" disabled={pending}>{pending ? "Validating…" : "Validate and save"}</button></div>
      </form>
    </dialog>
  );
}

function ResetDialog({ pending, error, onCancel, onConfirm }: { pending: boolean; error: string | null; onCancel: () => void; onConfirm: () => void }) {
  const dialogRef = useModalDialog();
  return <dialog ref={dialogRef} className="admin-dialog admin-confirm-dialog" aria-labelledby="reset-entitlements-title" onCancel={onCancel} onClose={onCancel}><div><p className="eyebrow">Confirm reset</p><h2 id="reset-entitlements-title">Reset entitlements?</h2><p>All tenant-specific feature and limit overrides will be removed. Runtime defaults will apply immediately.</p>{error && <p className="dialog-error" role="alert">{error}</p>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onCancel}>Cancel</button><button type="button" className="button button-danger" disabled={pending} onClick={onConfirm}>{pending ? "Resetting…" : "Reset to defaults"}</button></div></div></dialog>;
}

function useModalDialog() {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);
  return dialogRef;
}

const INVALID_LIMIT = Symbol("invalid-limit");

function parseLimit(row: LimitRow): EntitlementLimitValue | typeof INVALID_LIMIT {
  if (row.type === "unlimited") return null;
  if (row.type === "boolean") return row.value === "true";
  if (row.type === "text") return row.value;
  if (!row.value.trim()) return INVALID_LIMIT;
  const value = Number(row.value);
  return Number.isFinite(value) ? value : INVALID_LIMIT;
}

function limitRowValue(value: EntitlementLimitValue): Pick<LimitRow, "type" | "value"> {
  if (value === null) return { type: "unlimited", value: "" };
  if (typeof value === "boolean") return { type: "boolean", value: String(value) };
  if (typeof value === "number") return { type: "number", value: String(value) };
  return { type: "text", value };
}

function defaultLimitValue(type: LimitType) {
  return type === "boolean" ? "true" : "";
}

function formatLimit(value: EntitlementLimitValue) {
  if (value === null) return "Unlimited";
  if (typeof value === "boolean") return value ? "True" : "False";
  return typeof value === "number" ? value.toLocaleString() : value;
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function errorMessage(error: unknown) {
  if (error instanceof EntitlementsValidationError) return error.errors.join(" ");
  return error instanceof Error ? error.message : "The request failed. No changes were applied.";
}

class EntitlementsValidationError extends Error {
  readonly errors: string[];
  constructor(errors: string[]) {
    super("Entitlement validation failed");
    this.errors = errors;
  }
}
