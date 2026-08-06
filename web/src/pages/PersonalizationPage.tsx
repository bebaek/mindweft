import { type FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  type UserExecutionConfig,
  type UserExecutionConfigValidation,
  type UserExecutionCredential,
  type UserMCPAccess,
  type UserMCPStatus,
} from "../api/client";
import { useAuth } from "../auth/auth-context";
import { UserResourceEditors } from "../components/UserResourceEditors";

const starterConfig = {
  defaults: {},
  skills: { items: [] },
  mcp_servers: { items: [] },
  capability_profiles: { items: [] },
  agents: { items: [] },
};

export function PersonalizationPage() {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const configKey = ["user-execution-config", authentication];
  const credentialKey = ["user-execution-credentials", authentication];
  const config = useQuery({
    queryKey: configKey,
    queryFn: ({ signal }) => api.getUserExecutionConfig(signal),
    retry: false,
  });
  const credentials = useQuery({
    queryKey: credentialKey,
    queryFn: ({ signal }) => api.listUserExecutionCredentials(signal),
    retry: false,
  });
  const mcpAccess = useQuery({
    queryKey: ["user-mcp-access", authentication],
    queryFn: ({ signal }) => api.getUserMCPAccess(signal),
    retry: false,
  });
  const mcpStatus = useQuery({
    queryKey: ["user-mcp-status", authentication],
    queryFn: ({ signal }) => api.getUserMCPStatus(signal),
    retry: false,
  });
  const [draft, setDraft] = useState(pretty(starterConfig));
  const [dirty, setDirty] = useState(false);
  const [parseError, setParseError] = useState<string | null>(null);
  const [validation, setValidation] = useState<UserExecutionConfigValidation | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const missingConfig = config.error instanceof ApiError && config.error.status === 404;
  const configValue = !dirty && config.data ? pretty(config.data.config) : draft;

  const validate = useMutation({
    mutationFn: () => api.validateUserExecutionConfig(parseDraft(configValue)),
    onSuccess: setValidation,
  });
  const save = useMutation({
    mutationFn: () => api.updateUserExecutionConfig(parseDraft(configValue), config.data?.version),
    onSuccess: (saved) => {
      queryClient.setQueryData<UserExecutionConfig>(configKey, saved);
      setDraft(pretty(saved.config));
      setDirty(false);
      setParseError(null);
      setValidation({ valid: true, errors: [], normalized_config: saved.config });
      void queryClient.invalidateQueries({ queryKey: ["execution-options"] });
      void queryClient.invalidateQueries({ queryKey: ["user-resources"] });
    },
  });
  const reset = useMutation({
    mutationFn: () => api.deleteUserExecutionConfig(config.data?.version),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: configKey });
      setDraft(pretty(starterConfig));
      setDirty(false);
      setParseError(null);
      setValidation(null);
      setConfirmReset(false);
      void queryClient.invalidateQueries({ queryKey: ["execution-options"] });
      void queryClient.invalidateQueries({ queryKey: ["user-resources"] });
    },
  });

  function runValidation() {
    setParseError(null);
    setValidation(null);
    try {
      parseDraft(configValue);
      validate.mutate();
    } catch (error) {
      setParseError(errorMessage(error));
    }
  }

  function saveConfig() {
    setParseError(null);
    try {
      parseDraft(configValue);
      save.mutate();
    } catch (error) {
      setParseError(errorMessage(error));
    }
  }

  const configError = !missingConfig && config.error ? errorMessage(config.error) : null;
  const operationError = parseError || mutationError(validate.error) || mutationError(save.error) || mutationError(reset.error);

  return (
    <section className="personalization-page">
      <header className="personalization-heading">
        <div>
          <p className="eyebrow">Personal workspace</p>
          <h1>Personal agents and tools</h1>
          <p>Create private skills, MCP connections, capability profiles, and agent presets layered over your tenant configuration.</p>
        </div>
      </header>

      <MCPAccessPanel access={mcpAccess.data} status={mcpStatus.data} pending={mcpAccess.isPending || mcpStatus.isPending} error={mcpAccess.error ? errorMessage(mcpAccess.error) : mcpStatus.error ? errorMessage(mcpStatus.error) : null} />
      <UserResourceEditors />

      <div className="personalization-grid">
        <section className="personalization-panel config-panel" aria-labelledby="personal-config-title">
          <div className="personalization-panel-heading">
            <div>
              <p className="eyebrow">Execution overlay</p>
              <h2 id="personal-config-title">Personal configuration</h2>
              <p>Use qualified <code>user:</code> references for personal resources and <code>shared:</code> references for tenant resources.</p>
            </div>
            <span>{config.data ? `Version ${String(config.data.version)}` : "Not saved"}</span>
          </div>
          {config.isPending && <p className="personalization-status">Loading personal configuration…</p>}
          {configError && <p className="inline-error" role="alert">{configError}</p>}
          <label className="personal-config-editor">
            <span>Configuration JSON</span>
            <textarea
              spellCheck={false}
              value={configValue}
              onChange={(event) => {
                setDraft(event.target.value);
                setDirty(true);
                setParseError(null);
                setValidation(null);
              }}
              aria-describedby="personal-config-help"
            />
          </label>
          <p id="personal-config-help" className="personalization-help">Credential values do not belong here. Point an MCP server at a credential reference managed below.</p>
          {validation && (
            <div className={`personal-validation ${validation.valid ? "valid" : "invalid"}`} role="status">
              <strong>{validation.valid ? "Configuration is valid" : "Configuration needs attention"}</strong>
              {validation.errors.length > 0 && <ul>{validation.errors.map((error) => <li key={error}>{error}</li>)}</ul>}
            </div>
          )}
          {operationError && <p className="inline-error" role="alert">{operationError}</p>}
          {config.data && confirmReset && (
            <div className="personal-reset-confirm" role="alertdialog" aria-label="Confirm personal configuration reset">
              <p>Reset your personal configuration? Threads will fall back to tenant defaults, but saved credential values will remain.</p>
              <div><button type="button" onClick={() => setConfirmReset(false)}>Cancel</button><button type="button" className="button-danger" disabled={reset.isPending} onClick={() => reset.mutate()}>{reset.isPending ? "Resetting…" : "Reset configuration"}</button></div>
            </div>
          )}
          <div className="personalization-actions">
            {config.data && <button type="button" className="button button-danger" disabled={reset.isPending} onClick={() => setConfirmReset(true)}>Reset to tenant defaults</button>}
            <button type="button" className="button button-secondary" disabled={validate.isPending} onClick={runValidation}>{validate.isPending ? "Validating…" : "Validate"}</button>
            <button type="button" className="button button-primary" disabled={save.isPending || (!dirty && !missingConfig)} onClick={saveConfig}>{save.isPending ? "Saving…" : "Save configuration"}</button>
          </div>
        </section>

        <CredentialPanel
          credentials={credentials.data?.items ?? []}
          pending={credentials.isPending}
          error={credentials.error ? errorMessage(credentials.error) : null}
          onChanged={() => queryClient.invalidateQueries({ queryKey: credentialKey })}
        />
      </div>
    </section>
  );
}

function MCPAccessPanel({
  access,
  status,
  pending,
  error,
}: {
  access?: UserMCPAccess;
  status?: UserMCPStatus;
  pending: boolean;
  error: string | null;
}) {
  const [copied, setCopied] = useState<string | null>(null);
  const endpoint = access?.endpoint_path ?? status?.endpoint_path ?? "/user-mcp";
  const endpointUrl = `${window.location.origin}${endpoint}`;
  const servers = [...(access?.personal_servers ?? []), ...(access?.shared_servers ?? [])];

  async function copy(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
    } catch {
      setCopied(null);
    }
  }

  return (
    <section className="personalization-panel mcp-access-panel" aria-labelledby="mcp-access-title">
      <div className="personalization-panel-heading">
        <div>
          <p className="eyebrow">External agent connection</p>
          <h2 id="mcp-access-title">User MCP access</h2>
          <p>Connect an MCP-compatible client to your principal-scoped endpoint. Credentials and authorization headers are never shown here.</p>
        </div>
        {status && <span>{status.execution_configured ? "Ready" : "Setup needed"}</span>}
      </div>
      {pending && <p className="personalization-status">Loading MCP access…</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      {!pending && !error && (
        <>
          <div className="mcp-endpoint-row">
            <code>{endpointUrl}</code>
            <button type="button" onClick={() => void copy(endpointUrl, "endpoint")}>Copy endpoint</button>
          </div>
          <div className="mcp-access-meta">
            <span>{servers.length} server{servers.length === 1 ? "" : "s"} available</span>
            <span>{status?.personal_mcp_servers_allowed ? "Personal MCP allowed" : "Personal MCP blocked by tenant policy"}</span>
            {status?.execution_config_version != null && <span>Config version {String(status.execution_config_version)}</span>}
          </div>
          {status?.findings.map((finding) => (
            <p key={finding.code} className={`mcp-finding ${finding.severity}`} role="status">{finding.message} {finding.remediation}</p>
          ))}
          <div className="mcp-server-access-list">
            {servers.map((server) => (
              <div key={`${server.source}-${server.id}`} className="mcp-server-access-row">
                <div><strong>{server.name}</strong><small>{server.source === "user" ? "Personal" : "Shared"} · {server.allowed_tools?.join(", ") || "All allowed tools"}</small></div>
                {server.source === "user" && <span>{server.credential_configured ? "Credential configured" : "Credential missing"}</span>}
              </div>
            ))}
            {servers.length === 0 && <p className="personalization-empty">No effective MCP servers are available.</p>}
          </div>
          <div className="mcp-access-actions">
            <button type="button" onClick={() => void copy(`MCP endpoint: ${endpointUrl}\nUse your Minigent bearer authentication.`, "instructions")}>Copy client instructions</button>
            {copied && <small role="status">Copied {copied}</small>}
          </div>
        </>
      )}
    </section>
  );
}

function CredentialPanel({
  credentials,
  pending,
  error,
  onChanged,
}: {
  credentials: UserExecutionCredential[];
  pending: boolean;
  error: string | null;
  onChanged: () => Promise<unknown>;
}) {
  const { api } = useAuth();
  const [editing, setEditing] = useState<UserExecutionCredential | null>(null);
  const [credentialRef, setCredentialRef] = useState("");
  const [headerName, setHeaderName] = useState("Authorization");
  const [headerValue, setHeaderValue] = useState("");
  const [pendingDelete, setPendingDelete] = useState<UserExecutionCredential | null>(null);
  const save = useMutation({
    mutationFn: () => api.updateUserExecutionCredential(credentialRef.trim(), {
      header_name: headerName.trim(),
      header_value: headerValue,
      expected_version: editing?.version,
    }),
    onSuccess: async () => {
      clearForm();
      await onChanged();
    },
  });
  const remove = useMutation({
    mutationFn: (credential: UserExecutionCredential) => api.deleteUserExecutionCredential(credential.credential_ref, credential.version),
    onSuccess: async () => {
      setPendingDelete(null);
      await onChanged();
    },
  });

  function clearForm() {
    setEditing(null);
    setCredentialRef("");
    setHeaderName("Authorization");
    setHeaderValue("");
  }

  function editCredential(credential: UserExecutionCredential) {
    setEditing(credential);
    setCredentialRef(credential.credential_ref);
    setHeaderName(credential.header_name);
    setHeaderValue("");
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate();
  }

  return (
    <section className="personalization-panel credential-panel" aria-labelledby="personal-credentials-title">
      <div className="personalization-panel-heading">
        <div>
          <p className="eyebrow">Write-only secrets</p>
          <h2 id="personal-credentials-title">MCP credentials</h2>
          <p>Store one authorization or API-key header per reference. Existing values are never returned to the browser.</p>
        </div>
        <span>{String(credentials.length)} saved</span>
      </div>

      {pending && <p className="personalization-status">Loading credential metadata…</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      <ul className="personal-credential-list">
        {credentials.map((credential) => (
          <li key={credential.credential_ref}>
            <div><strong>{credential.credential_ref}</strong><small>{credential.header_name} · version {String(credential.version)}</small></div>
            <button type="button" onClick={() => editCredential(credential)}>Rotate</button>
            <button type="button" className="danger-link" onClick={() => setPendingDelete(credential)}>Delete</button>
          </li>
        ))}
        {!pending && !error && credentials.length === 0 && <li className="personalization-empty">No personal MCP credentials saved.</li>}
      </ul>

      {pendingDelete && (
        <div className="credential-delete-confirm" role="alertdialog" aria-label="Confirm credential deletion">
          <p>Delete <strong>{pendingDelete.credential_ref}</strong>? MCP servers using it will stop working.</p>
          <div><button type="button" onClick={() => setPendingDelete(null)}>Cancel</button><button type="button" className="button-danger" disabled={remove.isPending} onClick={() => remove.mutate(pendingDelete)}>{remove.isPending ? "Deleting…" : "Delete credential"}</button></div>
        </div>
      )}

      <form className="personal-credential-form" onSubmit={submit}>
        <div className="personal-credential-form-heading"><strong>{editing ? `Rotate ${editing.credential_ref}` : "Add credential"}</strong>{editing && <button type="button" onClick={clearForm}>Cancel</button>}</div>
        <label>Credential reference<input required disabled={Boolean(editing)} pattern="[A-Za-z0-9][A-Za-z0-9:._-]{0,255}" value={credentialRef} onChange={(event) => setCredentialRef(event.target.value)} placeholder="oauth:linear-primary" /></label>
        <label>HTTP header name<input required value={headerName} onChange={(event) => setHeaderName(event.target.value)} placeholder="Authorization" /></label>
        <label>Secret header value<input required type="password" autoComplete="new-password" value={headerValue} onChange={(event) => setHeaderValue(event.target.value)} placeholder={editing ? "Enter replacement value" : "Bearer token or API key"} /></label>
        <p>The value is encrypted server-side. Rotating it updates future runs without changing your configuration.</p>
        {save.isError && <p className="inline-error" role="alert">{errorMessage(save.error)}</p>}
        {remove.isError && <p className="inline-error" role="alert">{errorMessage(remove.error)}</p>}
        <button type="submit" className="button button-primary" disabled={save.isPending || !credentialRef.trim() || !headerName.trim() || !headerValue}>{save.isPending ? "Storing…" : editing ? "Rotate credential" : "Store credential"}</button>
      </form>
    </section>
  );
}

function parseDraft(value: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("Configuration must be valid JSON.");
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("Configuration must be a JSON object.");
  }
  return parsed as Record<string, unknown>;
}

function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function mutationError(error: unknown): string | null {
  return error ? errorMessage(error) : null;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "The operation could not be completed.";
}
