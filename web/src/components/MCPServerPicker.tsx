import { useState } from "react";
import type { AdminMcpServerCatalogItem, TenantMcpConnection } from "../api/client";
import {
  parseMcpServers,
  serverName,
  toggleMcpServerPreset,
} from "./mcpServerPresets";

export function MCPServerPicker({
  value,
  catalog,
  pending,
  error,
  onChange,
  onReplaceCredential,
  credentialDisabled = false,
  connections = [],
  onTestConnection,
}: {
  value: string;
  catalog: AdminMcpServerCatalogItem[];
  pending: boolean;
  error: string | null;
  onChange: (value: string) => void;
  onReplaceCredential?: (serverName: string, token: string) => Promise<void>;
  credentialDisabled?: boolean;
  connections?: TenantMcpConnection[];
  onTestConnection?: (name: string) => Promise<void>;
}) {
  const parsed = parseMcpServers(value);
  const invalid = parsed === null;

  return <section className="mcp-server-picker" aria-labelledby="internal-mcp-title">
    <div className="mcp-server-picker-heading">
      <div><strong id="internal-mcp-title">Internal services</strong><span>Enable tools advertised by this Mindweft deployment.</span></div>
      <span className="mcp-server-picker-badge">Quick add</span>
    </div>
    {pending && <p className="mcp-server-picker-status">Loading available services…</p>}
    {error && <p className="mcp-server-picker-error" role="alert">{error}</p>}
    {!pending && !error && catalog.length === 0 && <p className="mcp-server-picker-status">No internal services are configured for this deployment.</p>}
    <div className="mcp-server-preset-list">
      {catalog.map((preset) => {
        const presetName = serverName(preset.server);
        const enabled = parsed?.some((server) => serverName(server) === presetName) ?? false;
        return <article className="mcp-server-preset mcp-server-credential-preset" key={preset.id}>
          <div><strong>{preset.title}</strong><p>{preset.description}</p>{preset.detail && <small>{preset.detail}</small>}</div>
          <button
            type="button"
            className={enabled ? "enabled" : ""}
            disabled={invalid}
            aria-pressed={enabled}
            onClick={() => onChange(toggleMcpServerPreset(value, preset, !enabled))}
          >{enabled ? "Enabled — remove" : "Enable"}</button>
          {onTestConnection && <ConnectionInspection
            connection={connections.find(connection => connection.name === presetName)}
            disabled={credentialDisabled || !enabled} title={preset.title}
            onTest={() => onTestConnection(presetName)} />}
          {enabled && preset.tenant_credential === "bearer" && onReplaceCredential &&
            <BearerCredentialEditor key={presetName} title={preset.title}
              disabled={credentialDisabled}
              onReplace={(token) => onReplaceCredential(presetName, token)} />}
        </article>;
      })}
    </div>
    {invalid && <p className="mcp-server-picker-error" role="alert">Fix the advanced MCP JSON before changing internal services.</p>}
  </section>;
}

function BearerCredentialEditor({ title, disabled, onReplace }: {
  title: string; disabled: boolean; onReplace: (token: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function replace() {
    setPending(true);
    setError(null);
    try {
      await onReplace(token);
      setToken("");
      setOpen(false);
    } catch {
      // Do not display an arbitrary upstream exception that could echo credentials.
      setError("Token was not saved. Check the token and connection, or reopen the editor if the configuration changed.");
    } finally {
      setPending(false);
    }
  }
  return <div className="mcp-credential-editor" onChange={(event) => event.stopPropagation()}>
    <small>Tenant-managed bearer credential</small>
    {!open ? <button type="button" disabled={disabled} onClick={() => setOpen(true)}>Replace {title} token</button> : <>
      <label>New {title} token<input type="password" autoComplete="off" value={token}
        disabled={pending || disabled} onChange={(event) => setToken(event.target.value)} /></label>
      <small>Enter the raw token, without Bearer. Validation only discovers tools; it does not read financial data. Saving closes this editor.</small>
      {error && <p role="alert">{error}</p>}
      <button type="button" disabled={pending} onClick={() => { setToken(""); setError(null); setOpen(false); }}>Cancel token replacement</button>
      <button type="button" disabled={pending || disabled || !token || /\s/.test(token)} onClick={() => void replace()}>{pending ? "Validating…" : "Validate and save token"}</button>
    </>}
    {disabled && <small>Save or discard other configuration edits before replacing a token.</small>}
  </div>;
}

function ConnectionInspection({ connection, disabled, title, onTest }: {
  connection?: TenantMcpConnection; disabled: boolean; title: string; onTest: () => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(false);
  const check = connection?.last_check;
  async function testConnection() {
    setPending(true);
    setError(false);
    try { await onTest(); }
    catch { setError(true); }
    finally { setPending(false); }
  }
  return <div className="mcp-credential-editor">
    <small>Saved connection: {connection ? connection.enabled ? "enabled" : "not connected" : "status unavailable"}</small>
    {connection && <small>Credential ownership: {connection.credential_owner}{connection.credential_owner === "tenant" ? ` · Bearer ${connection.bearer_configured ? "configured (not proof of validity)" : "missing"}` : ""}</small>}
    <small>{check ? `Last check ${check.status} · ${new Date(check.checked_at).toLocaleString()}` : "No check recorded for the current configuration"}</small>
    {check?.status === "succeeded" && <details><summary>Permitted tools discovered ({check.tools.length})</summary><ul>{check.tools.map(tool => <li key={tool}>{tool}</li>)}</ul></details>}
    <button type="button" disabled={pending || disabled || !connection?.enabled} onClick={() => void testConnection()}>{pending ? "Testing connection…" : `Test ${title} connection`}</button>
    {error && <p role="alert">Connection check could not complete. Reload and retry.</p>}
    {check?.status === "failed" && <small>The last discovery check failed. Check credentials and service availability; no tool was invoked.</small>}
  </div>;
}
