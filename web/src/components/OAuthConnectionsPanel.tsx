import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../auth/auth-context";

export function OAuthConnectionsPanel() {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const connections = useQuery({ queryKey: ["oauth-connections", authentication], queryFn: () => api.listOAuthConnections(), retry: false });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [disconnect, setDisconnect] = useState<string | null>(null);
  const [importName, setImportName] = useState<string | null>(null);
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError(null);
    try { await action(); await queryClient.invalidateQueries({ queryKey: ["oauth-connections"] }); }
    catch (error) { setError(error instanceof Error ? error.message : "OAuth operation failed"); }
    finally { setBusy(false); }
  }
  return <section className="personalization-panel" aria-labelledby="oauth-connections-title">
    <h3 id="oauth-connections-title">Named OAuth connections</h3>
    <p>Separate accounts can share a provider. LLM profiles reference these connections by name. Status reflects stored credentials only, not live provider access.</p>
    {connections.error && <p role="alert">{connections.error.message}</p>}
    {error && <p role="alert">{error}</p>}
    {connections.data?.items.length === 0 && <p>No named connections configured. Add deployment-owned oauth.connections definitions first.</p>}
    {connections.data?.items.map((connection) => <div key={connection.name}>
      <strong>{connection.name}</strong> · {connection.provider_id} · {connection.connected ? connection.expired ? "Stored credentials expired; refresh will be attempted on use" : "Credentials stored (unverified)" : "Not connected"}
      {connection.account_id && <small> · Account: {connection.account_id}</small>}
      <button type="button" disabled={busy} onClick={() => void perform(async () => { const result = await api.loginOAuthConnection(connection.name); window.location.assign(result.authorization_url); })}>{connection.connected ? "Reconnect" : "Connect"} {connection.name}</button>
      <button type="button" disabled={busy} onClick={() => setDisconnect(connection.name)}>Disconnect {connection.name}</button>
      {connection.provider_id === "openai-codex" && <button type="button" disabled={busy} onClick={() => setImportName(connection.name)}>Import Pi credentials for {connection.name}</button>}
    </div>)}
    {disconnect && <div><p>Disconnect {disconnect}? This invalidates pending logins and local credentials for this connection only. It does not revoke the account at the provider.</p><button type="button" disabled={busy} onClick={() => void perform(async () => { await api.disconnectOAuthConnection(disconnect); setDisconnect(null); })}>Confirm disconnect</button><button type="button" disabled={busy} onClick={() => setDisconnect(null)}>Cancel</button></div>}
    {importName && <label>Import credentials into {importName}. Selecting a file transfers its reusable tokens into this tenant's encrypted store; do not import another person's account.
      <input type="file" accept="application/json,.json" disabled={busy} onChange={(event) => {
        const file = event.target.files?.[0]; event.target.value = "";
        if (!file) return;
        void perform(async () => {
          if (file.size > 256 * 1024) throw new Error("Credential file exceeds 256 KiB");
          let payload: unknown;
          try { payload = JSON.parse(await file.text()); } catch { throw new Error("Invalid credential JSON"); }
          if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("Invalid credential JSON");
          const object = payload as Record<string, unknown>;
          const credential = object["openai-codex"] ?? object;
          if (!credential || typeof credential !== "object" || Array.isArray(credential)) throw new Error("Invalid Pi credential");
          await api.importOAuthConnection(importName, credential as Record<string, unknown>);
          setImportName(null);
        });
      }} />
    </label>}
  </section>;
}
