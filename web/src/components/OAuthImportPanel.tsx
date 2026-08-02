import { useRef, useState, type ChangeEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, type TenantOAuthCredentialStatus } from "../api/client";
import { useAuth } from "../auth/auth-context";

const MAX_AUTH_FILE_BYTES = 256 * 1024;

export function OAuthImportPanel({ tenantId }: { tenantId: string }) {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const queryKey = ["tenant-openai-oauth", tenantId, authentication];
  const credential = useQuery({
    queryKey,
    queryFn: ({ signal }) => api.getTenantOpenAIOAuthCredential(tenantId, signal),
    retry: false,
  });
  const importCredential = useMutation({
    mutationFn: (value: Record<string, unknown>) =>
      api.importTenantOpenAIOAuthFromPi(tenantId, value),
    onSuccess: (status) => {
      queryClient.setQueryData<TenantOAuthCredentialStatus>(queryKey, status);
      setLocalError(null);
    },
  });
  const disconnect = useMutation({
    mutationFn: () => api.deleteTenantOpenAIOAuthCredential(tenantId),
    onSuccess: () => {
      queryClient.setQueryData<TenantOAuthCredentialStatus>(queryKey, (current) => ({
        tenant_id: tenantId,
        provider_id: current?.provider_id ?? "openai-codex",
        source: "pi",
        connected: false,
      }));
      setConfirmDisconnect(false);
    },
  });

  async function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    importCredential.reset();
    setLocalError(null);
    if (!acknowledged) {
      setLocalError("Acknowledge the credential transfer warning before importing.");
      return;
    }
    if (file.size > MAX_AUTH_FILE_BYTES) {
      setLocalError("Pi auth.json must be smaller than 256 KiB.");
      return;
    }
    try {
      const parsed: unknown = JSON.parse(await file.text());
      if (!isObject(parsed)) throw new Error("Pi auth.json must contain a JSON object.");
      const entry = parsed["openai-codex"];
      if (!isObject(entry)) {
        throw new Error("Pi auth.json does not contain an openai-codex credential. Run /login in Pi first.");
      }
      importCredential.mutate(entry);
    } catch (error) {
      setLocalError(errorMessage(error));
    }
  }

  const error = localError
    || (importCredential.isError ? errorMessage(importCredential.error) : null)
    || (disconnect.isError ? errorMessage(disconnect.error) : null);

  return (
    <section className="oauth-import-panel" aria-labelledby="oauth-import-title">
      <div className="oauth-import-heading">
        <div>
          <p className="eyebrow">OpenAI OAuth</p>
          <h3 id="oauth-import-title">Import from Pi</h3>
          <p>Transfer a ChatGPT Plus/Pro OpenAI Codex credential from Pi without using a hosted OAuth callback.</p>
        </div>
        {credential.data?.connected && <span className="oauth-connected">Connected</span>}
      </div>

      {credential.isPending && <p className="oauth-import-loading">Checking OAuth connection…</p>}
      {credential.isError && <p className="inline-error" role="alert">{errorMessage(credential.error)}</p>}

      {credential.data?.connected ? (
        <div className="oauth-credential-summary">
          <div><span>Source</span><strong>Pi openai-codex</strong></div>
          <div><span>Account</span><strong>{credential.data.account_id || "Available"}</strong></div>
          <div><span>Access token expires</span><strong>{formatDate(credential.data.expires_at)}</strong></div>
          <p>Minigent refreshes this credential for this tenant. Continuing to use the same credential in Pi may invalidate one copy when refresh tokens rotate.</p>
        </div>
      ) : (
        <div className="oauth-import-instructions">
          <ol>
            <li>In Pi, run <code>/login</code> and select <strong>OpenAI (ChatGPT Plus/Pro)</strong>.</li>
            <li>Locate <code>~/.pi/agent/auth.json</code> on the machine where Pi is installed.</li>
            <li>Transfer the credential to Minigent, then avoid using the same Pi credential concurrently.</li>
            <li>In Execution configuration, use provider <code>generic-oauth</code> and the Codex Responses URL <code>https://chatgpt.com/backend-api/codex/responses</code>.</li>
          </ol>
        </div>
      )}

      <label className="oauth-transfer-warning">
        <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
        <span>I understand this copies a rotating OAuth credential. Pi and Minigent should not refresh the same credential concurrently.</span>
      </label>

      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="oauth-import-actions">
        <input ref={fileInput} aria-label="Pi auth.json" className="sr-only" type="file" accept="application/json,.json" onChange={(event) => void selectFile(event)} />
        <button type="button" disabled={!acknowledged || importCredential.isPending} onClick={() => fileInput.current?.click()}>
          {importCredential.isPending ? "Importing…" : credential.data?.connected ? "Replace from Pi auth.json" : "Import Pi auth.json"}
        </button>
        {credential.data?.connected && !confirmDisconnect && <button type="button" className="danger-link" onClick={() => setConfirmDisconnect(true)}>Disconnect</button>}
      </div>

      {confirmDisconnect && (
        <div className="oauth-disconnect-confirm" role="alertdialog" aria-label="Disconnect OpenAI OAuth">
          <p>Remove this tenant&apos;s imported OpenAI OAuth credential?</p>
          <button type="button" onClick={() => setConfirmDisconnect(false)}>Cancel</button>
          <button type="button" className="danger-link" disabled={disconnect.isPending} onClick={() => disconnect.mutate()}>{disconnect.isPending ? "Disconnecting…" : "Disconnect"}</button>
        </div>
      )}
    </section>
  );
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatDate(value?: string | null): string {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "OAuth credential operation failed.";
}
