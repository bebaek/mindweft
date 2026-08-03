import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  type AdminExecutionValidation,
  type AdminTenantExecutionConfig,
} from "../api/client";
import { useAuth } from "../auth/auth-context";
import { MCPServerPicker } from "./MCPServerPicker";

type EditorTab = "llm" | "tools" | "runtime" | "skills" | "presets" | "advanced";

type ValidationResult = {
  report: AdminExecutionValidation;
  saved: AdminTenantExecutionConfig | null;
};

export function ExecutionConfigPanel({ tenantId }: { tenantId: string }) {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [validation, setValidation] = useState<AdminExecutionValidation | null>(null);
  const executionConfig = useQuery({
    queryKey: ["admin-tenant-execution-config", tenantId, authentication],
    queryFn: ({ signal }) => api.getAdminTenantExecutionConfig(tenantId, signal),
    retry: false,
  });
  const missing = executionConfig.error instanceof ApiError && executionConfig.error.status === 404;
  const apply = useMutation({
    mutationFn: async (config: Record<string, unknown>): Promise<ValidationResult> => {
      const report = await api.validateAdminTenantExecutionConfig(tenantId, config);
      if (!report.valid) return { report, saved: null };
      const saved = await api.updateAdminTenantExecutionConfig(tenantId, config);
      return { report, saved };
    },
    onSuccess: ({ report, saved }) => {
      setValidation(report);
      if (!saved) return;
      queryClient.setQueryData(
        ["admin-tenant-execution-config", tenantId, authentication],
        saved,
      );
      setEditing(false);
    },
  });
  const reset = useMutation({
    mutationFn: () => api.deleteAdminTenantExecutionConfig(tenantId),
    onSuccess: () => {
      queryClient.removeQueries({
        queryKey: ["admin-tenant-execution-config", tenantId, authentication],
        exact: true,
      });
      setConfirmReset(false);
      void queryClient.invalidateQueries({
        queryKey: ["admin-tenant-execution-config", tenantId],
      });
    },
  });

  function openEditor() {
    apply.reset();
    setValidation(null);
    setEditing(true);
  }

  return (
    <section className="execution-config-panel" aria-labelledby="execution-config-title">
      <div className="execution-config-heading">
        <div><p className="eyebrow">Runtime policy</p><h3 id="execution-config-title">Execution configuration</h3><p>Configure models, tools, agent behavior, skills, and capability presets.</p></div>
        <div className="execution-config-heading-actions">
          {executionConfig.data && <span>Version {executionConfig.data.version}</span>}
          <button type="button" onClick={openEditor}>{executionConfig.data ? "Edit configuration" : "Configure runtime"}</button>
        </div>
      </div>

      {executionConfig.isPending && <p className="execution-config-loading">Loading execution configuration…</p>}
      {executionConfig.isError && !missing && <p className="inline-error" role="alert">{errorMessage(executionConfig.error)}</p>}
      {missing && <div className="execution-config-empty"><strong>No tenant-specific execution configuration</strong><span>The configured tenant source will use defaults or fail closed, depending on server policy.</span></div>}
      {executionConfig.data && <ExecutionSummary config={executionConfig.data.config} />}
      {executionConfig.data && <footer className="execution-config-footer"><span>Secrets are redacted and never returned to the browser.</span><button type="button" onClick={() => { reset.reset(); setConfirmReset(true); }}>Reset configuration</button></footer>}

      {editing && <ExecutionConfigEditor key={`${tenantId}-${executionConfig.data?.version ?? "new"}`} current={executionConfig.data?.config ?? null} pending={apply.isPending} validation={validation} error={apply.isError ? errorMessage(apply.error) : null} onClose={() => { apply.reset(); setValidation(null); setEditing(false); }} onApply={(config) => { setValidation(null); apply.mutate(config); }} />}
      {confirmReset && <ResetExecutionConfigDialog pending={reset.isPending} error={reset.isError ? errorMessage(reset.error) : null} onCancel={() => setConfirmReset(false)} onConfirm={() => reset.mutate()} />}
    </section>
  );
}

function ExecutionSummary({ config }: { config: Record<string, unknown> }) {
  const llm = asObject(config.llm);
  const tools = asObject(config.tools);
  const backend = asObject(config.agent_backend ?? config.agentBackend);
  const mcpServers = asArray(tools.mcp_servers ?? tools.mcpServers);
  const localTools = asArray(tools.allowed_local_tools ?? tools.allowedLocalTools);
  const skills = asArray(asObject(config.skills).items);
  const profiles = asArray(asObject(config.capability_profiles ?? config.capabilityProfiles).items);
  const agents = asArray(asObject(config.agents ?? config.agent_presets ?? config.agentPresets).items);
  return <div className="execution-summary"><SummaryItem label="LLM" value={stringValue(llm.provider) || "mock"} detail={stringValue(llm.model) || "Default model"} /><SummaryItem label="Tools" value={String(localTools.length)} detail={`${String(mcpServers.length)} MCP servers`} /><SummaryItem label="Backend" value={stringValue(backend.type) || "native"} detail={stringValue(backend.peer) || "Local runtime"} /><SummaryItem label="Skills" value={String(skills.length)} detail={`${String(profiles.length)} capability profiles`} /><SummaryItem label="Agents" value={String(agents.length)} detail="Named presets" /></div>;
}

function SummaryItem({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <article><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>;
}

function ExecutionConfigEditor({ current, pending, validation, error, onClose, onApply }: { current: Record<string, unknown> | null; pending: boolean; validation: AdminExecutionValidation | null; error: string | null; onClose: () => void; onApply: (config: Record<string, unknown>) => void }) {
  const dialogRef = useModalDialog();
  const initial = useMemo(() => structuredConfig(current), [current]);
  const [tab, setTab] = useState<EditorTab>("llm");
  const [dirty, setDirty] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [provider, setProvider] = useState(initial.provider);
  const [model, setModel] = useState(initial.model);
  const [baseUrl, setBaseUrl] = useState(initial.baseUrl);
  const [apiKey, setApiKey] = useState("");
  const [clearApiKey, setClearApiKey] = useState(false);
  const [timeout, setTimeout] = useState(initial.timeout);
  const [localTools, setLocalTools] = useState(initial.localTools);
  const [backendType, setBackendType] = useState(initial.backendType);
  const [peer, setPeer] = useState(initial.peer);
  const [cwd, setCwd] = useState(initial.cwd);
  const [llmAdvanced, setLlmAdvanced] = useState(initial.llmAdvanced);
  const [mcpServers, setMcpServers] = useState(initial.mcpServers);
  const [toolsAdvanced, setToolsAdvanced] = useState(initial.toolsAdvanced);
  const [backendAdvanced, setBackendAdvanced] = useState(initial.backendAdvanced);
  const [quality, setQuality] = useState(initial.quality);
  const [skills, setSkills] = useState(initial.skills);
  const [capabilities, setCapabilities] = useState(initial.capabilities);
  const [agents, setAgents] = useState(initial.agents);
  const [llmProfiles, setLlmProfiles] = useState(initial.llmProfiles);
  const [defaultLlmProfile, setDefaultLlmProfile] = useState(initial.defaultLlmProfile);
  const [topLevelAdvanced, setTopLevelAdvanced] = useState(initial.topLevelAdvanced);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  function requestClose() {
    if (dirty) setConfirmDiscard(true);
    else onClose();
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    try {
      const config = parseEditorConfig({
        provider, model, baseUrl, apiKey, clearApiKey, hadApiKey: initial.hadApiKey,
        timeout, localTools, backendType, peer, cwd, llmAdvanced, mcpServers,
        toolsAdvanced, backendAdvanced, quality, skills, capabilities, agents,
        llmProfiles, defaultLlmProfile, topLevelAdvanced,
      });
      setLocalError(null);
      onApply(config);
    } catch (caught) {
      setLocalError(errorMessage(caught));
    }
  }

  return <dialog ref={dialogRef} className="admin-dialog execution-config-dialog" aria-labelledby="execution-editor-title" onCancel={(event) => { event.preventDefault(); requestClose(); }}>
    <form onSubmit={submit} onChangeCapture={() => setDirty(true)}>
      <header className="dialog-heading"><div><p className="eyebrow">Runtime policy</p><h2 id="execution-editor-title">{current ? "Edit execution configuration" : "Configure runtime"}</h2></div><button type="button" className="icon-button" aria-label="Close" onClick={requestClose}>×</button></header>
      <p className="execution-editor-copy">Validate the complete configuration before applying it. Stored secrets remain server-side unless replaced or explicitly cleared.</p>
      <nav className="execution-editor-tabs" aria-label="Execution configuration sections">{(["llm", "tools", "runtime", "skills", "presets", "advanced"] as EditorTab[]).map((item) => <button key={item} type="button" className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{tabLabel(item)}</button>)}</nav>

      {tab === "llm" && <div className="execution-editor-section"><div className="oauth-llm-preset"><div><strong>OpenAI through Pi OAuth</strong><span>Uses the tenant credential imported from Pi. Choose a Codex model before applying.</span></div><button type="button" onClick={() => { setProvider("generic-oauth"); setBaseUrl("https://chatgpt.com/backend-api/codex/responses"); setApiKey(""); setClearApiKey(false); setDirty(true); }}>Use OAuth defaults</button></div><div className="execution-field-grid"><label>Provider<input required value={provider} onChange={(event) => setProvider(event.target.value)} placeholder="mock" /></label><label>Model<input value={model} onChange={(event) => setModel(event.target.value)} placeholder="Provider default" /></label><label className="wide">Base URL<input type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="Provider default" /></label><label>Timeout (seconds)<input required type="number" min="0.1" step="0.1" value={timeout} onChange={(event) => setTimeout(event.target.value)} /></label><label>API key<input type="password" autoComplete="off" value={apiKey} disabled={clearApiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={initial.hadApiKey ? "Stored secret — leave blank to preserve" : "Optional"} /></label>{initial.hadApiKey && <label className="execution-checkbox"><input type="checkbox" checked={clearApiKey} onChange={(event) => setClearApiKey(event.target.checked)} />Clear stored API key</label>}</div><JsonField label="Advanced LLM settings" value={llmAdvanced} onChange={setLlmAdvanced} help="JSON object for headers, token limits, thinking, caching, and input modalities." /></div>}
      {tab === "tools" && <div className="execution-editor-section"><label className="execution-text-field">Allowed local tools<input value={localTools} onChange={(event) => setLocalTools(event.target.value)} placeholder="echo, current_time, calculator" /><small>Comma-separated. Leave blank to use the runtime default tool policy.</small></label><MCPServerPicker value={mcpServers} onChange={(value) => { setMcpServers(value); setDirty(true); }} /><details className="execution-advanced"><summary>Advanced MCP server JSON</summary><JsonField label="MCP servers" value={mcpServers} onChange={setMcpServers} help="Add custom server definitions or edit quick-added services. Credentials belong in headers; redacted values preserve stored secrets." rows={10} /></details><JsonField label="Advanced tool policy" value={toolsAdvanced} onChange={setToolsAdvanced} help="JSON object for result redaction and other tool policy fields." /></div>}
      {tab === "runtime" && <div className="execution-editor-section"><div className="execution-field-grid"><label>Backend<select value={backendType} onChange={(event) => setBackendType(event.target.value)}><option value="native">Native</option><option value="peer_agent">Peer agent</option></select></label>{backendType === "peer_agent" && <><label>Peer<input required value={peer} onChange={(event) => setPeer(event.target.value)} placeholder="pi" /></label><label className="wide">Working directory<input required value={cwd} onChange={(event) => setCwd(event.target.value)} placeholder="/workspace/project" /></label></>}</div><JsonField label="Advanced backend settings" value={backendAdvanced} onChange={setBackendAdvanced} help="JSON object for timeouts, polling, and MCP broker behavior." /><JsonField label="Quality review" value={quality} onChange={setQuality} help="JSON object for optional remote quality review. Secret values stay redacted." /></div>}
      {tab === "skills" && <div className="execution-editor-section"><JsonField label="Skills configuration" value={skills} onChange={setSkills} help="JSON object with default_skill and items. Items can use system_prompt or instruction_source." rows={16} /></div>}
      {tab === "presets" && <div className="execution-editor-section"><JsonField label="Capability profiles" value={capabilities} onChange={setCapabilities} help="JSON object with default_profile and tool-narrowing items." rows={11} /><JsonField label="Agent presets" value={agents} onChange={setAgents} help="JSON object whose items combine skills and capability profiles." rows={11} /></div>}
      {tab === "advanced" && <div className="execution-editor-section"><label className="execution-text-field">Default LLM profile<input value={defaultLlmProfile} onChange={(event) => setDefaultLlmProfile(event.target.value)} placeholder="Optional profile name" /></label><JsonField label="LLM profiles" value={llmProfiles} onChange={setLlmProfiles} help="JSON object of named LLM configurations." rows={12} /><JsonField label="Other top-level configuration" value={topLevelAdvanced} onChange={setTopLevelAdvanced} help="JSON object for supported fields not represented in the guided sections." /></div>}

      {validation && <ValidationReport report={validation} />}
      {(localError || error) && <p className="dialog-error" role="alert">{localError || error}</p>}
      {confirmDiscard && <div className="execution-discard-warning" role="alertdialog" aria-label="Discard execution configuration changes"><p>Discard unsaved configuration changes?</p><button type="button" onClick={() => setConfirmDiscard(false)}>Keep editing</button><button type="button" className="danger" onClick={onClose}>Discard changes</button></div>}
      <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={requestClose}>Cancel</button><button type="submit" className="button button-primary" disabled={pending}>{pending ? "Validating…" : "Validate and apply"}</button></div>
    </form>
  </dialog>;
}

function JsonField({ label, value, onChange, help, rows = 6 }: { label: string; value: string; onChange: (value: string) => void; help: string; rows?: number }) {
  return <label className="execution-json-field">{label}<textarea spellCheck={false} rows={rows} value={value} onChange={(event) => onChange(event.target.value)} /><small>{help}</small></label>;
}

function ValidationReport({ report }: { report: AdminExecutionValidation }) {
  const errors = [...report.config_shape.errors, ...report.llm.errors, ...report.tools.errors];
  return <div className={`execution-validation ${report.valid ? "valid" : "invalid"}`} role="status"><strong>{report.valid ? "Validation passed" : "Validation needs attention"}</strong>{errors.length > 0 && <ul>{errors.map((item, index) => <li key={`${String(index)}-${item}`}>{item}</li>)}</ul>}<div><span>Shape: {report.config_shape.ok ? "OK" : "Failed"}</span><span>LLM: {report.llm.ok ? "OK" : "Failed"}</span><span>Tools: {report.tools.ok ? "OK" : "Failed"}</span></div>{report.tools.mcp_servers.length > 0 && <details><summary>MCP checks</summary><ul>{report.tools.mcp_servers.map((server) => <li key={server.name}>{server.name}: {server.ok ? `${String(server.tool_count)} tools` : server.error || "Failed"}</li>)}</ul></details>}</div>;
}

function ResetExecutionConfigDialog({ pending, error, onCancel, onConfirm }: { pending: boolean; error: string | null; onCancel: () => void; onConfirm: () => void }) {
  const dialogRef = useModalDialog();
  return <dialog ref={dialogRef} className="admin-dialog admin-confirm-dialog" aria-labelledby="reset-execution-title" onCancel={onCancel} onClose={onCancel}><div><p className="eyebrow">Confirm reset</p><h2 id="reset-execution-title">Reset execution configuration?</h2><p>The stored tenant configuration will be deleted. Runtime defaults will apply, or execution will fail closed when the server uses store-only configuration.</p>{error && <p className="dialog-error" role="alert">{error}</p>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onCancel}>Cancel</button><button type="button" className="button button-danger" disabled={pending} onClick={onConfirm}>{pending ? "Resetting…" : "Reset configuration"}</button></div></div></dialog>;
}

interface EditorValues {
  provider: string; model: string; baseUrl: string; apiKey: string; clearApiKey: boolean;
  hadApiKey: boolean; timeout: string; localTools: string; backendType: string; peer: string;
  cwd: string; llmAdvanced: string; mcpServers: string; toolsAdvanced: string;
  backendAdvanced: string; quality: string; skills: string; capabilities: string;
  agents: string; llmProfiles: string; defaultLlmProfile: string; topLevelAdvanced: string;
}

function parseEditorConfig(values: EditorValues): Record<string, unknown> {
  const config = parseJsonObject(values.topLevelAdvanced, "Other top-level configuration");
  const llm = parseJsonObject(values.llmAdvanced, "Advanced LLM settings");
  llm.provider = values.provider.trim();
  setOptional(llm, "model", values.model);
  setOptional(llm, "base_url", values.baseUrl);
  const timeout = Number(values.timeout);
  if (!Number.isFinite(timeout) || timeout <= 0) throw new Error("LLM timeout must be a positive number.");
  llm.timeout = timeout;
  if (values.clearApiKey) llm.api_key = null;
  else if (values.apiKey) llm.api_key = values.apiKey;
  else if (values.hadApiKey) llm.api_key = "<redacted>";
  config.llm = llm;

  const tools = parseJsonObject(values.toolsAdvanced, "Advanced tool policy");
  const parsedTools = values.localTools.split(",").map((item) => item.trim()).filter(Boolean);
  if (parsedTools.length) tools.allowed_local_tools = [...new Set(parsedTools)];
  tools.mcp_servers = parseJsonArray(values.mcpServers, "MCP servers");
  config.tools = tools;

  const backend = parseJsonObject(values.backendAdvanced, "Advanced backend settings");
  backend.type = values.backendType;
  if (values.backendType === "peer_agent") {
    backend.peer = values.peer.trim();
    backend.cwd = values.cwd.trim();
  } else {
    delete backend.peer;
    delete backend.cwd;
  }
  config.agent_backend = backend;
  config.quality = parseJsonObject(values.quality, "Quality review");
  config.skills = parseJsonObject(values.skills, "Skills configuration");
  config.capability_profiles = parseJsonObject(values.capabilities, "Capability profiles");
  config.agents = parseJsonObject(values.agents, "Agent presets");
  config.llm_profiles = parseJsonObject(values.llmProfiles, "LLM profiles");
  setOptional(config, "default_llm_profile", values.defaultLlmProfile);
  return config;
}

function structuredConfig(current: Record<string, unknown> | null) {
  const config = cloneObject(current ?? {});
  const llm = cloneObject(asObject(config.llm));
  const tools = cloneObject(asObject(config.tools));
  const backend = cloneObject(asObject(config.agent_backend ?? config.agentBackend));
  const provider = stringValue(llm.provider) || "mock";
  const model = stringValue(llm.model);
  const baseUrl = stringValue(llm.base_url ?? llm.baseUrl);
  const timeout = String(typeof llm.timeout === "number" ? llm.timeout : 30);
  const hadApiKey = llm.has_api_key === true || llm.api_key === "<redacted>" || llm.apiKey === "<redacted>";
  removeKeys(llm, ["provider", "model", "base_url", "baseUrl", "api_key", "apiKey", "has_api_key", "timeout"]);
  const localTools = asArray(tools.allowed_local_tools ?? tools.allowedLocalTools).filter((item): item is string => typeof item === "string").join(", ");
  const mcp = tools.mcp_servers ?? tools.mcpServers ?? [];
  removeKeys(tools, ["allowed_local_tools", "allowedLocalTools", "mcp_servers", "mcpServers"]);
  const backendType = stringValue(backend.type) || "native";
  const peer = stringValue(backend.peer);
  const cwd = stringValue(backend.cwd);
  removeKeys(backend, ["type", "peer", "cwd"]);
  const quality = config.quality ?? {};
  const skills = config.skills ?? { items: [] };
  const capabilities = config.capability_profiles ?? config.capabilityProfiles ?? { items: [] };
  const agents = config.agents ?? config.agent_presets ?? config.agentPresets ?? { items: [] };
  const llmProfiles = config.llm_profiles ?? config.llmProfiles ?? {};
  const defaultLlmProfile = stringValue(config.default_llm_profile ?? config.defaultLlmProfile);
  removeKeys(config, ["llm", "tools", "agent_backend", "agentBackend", "quality", "skills", "capability_profiles", "capabilityProfiles", "agents", "agent_presets", "agentPresets", "llm_profiles", "llmProfiles", "default_llm_profile", "defaultLlmProfile"]);
  return {
    provider, model, baseUrl, timeout, hadApiKey, localTools, backendType, peer, cwd,
    llmAdvanced: pretty(llm), mcpServers: pretty(mcp), toolsAdvanced: pretty(tools),
    backendAdvanced: pretty(backend), quality: pretty(quality), skills: pretty(skills),
    capabilities: pretty(capabilities), agents: pretty(agents), llmProfiles: pretty(llmProfiles),
    defaultLlmProfile, topLevelAdvanced: pretty(config),
  };
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  const parsed = parseJson(value, label);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error(`${label} must be a JSON object.`);
  return parsed as Record<string, unknown>;
}

function parseJsonArray(value: string, label: string): unknown[] {
  const parsed = parseJson(value, label);
  if (!Array.isArray(parsed)) throw new Error(`${label} must be a JSON array.`);
  return parsed;
}

function parseJson(value: string, label: string): unknown {
  try { return JSON.parse(value) as unknown; }
  catch { throw new Error(`${label} contains invalid JSON.`); }
}

function useModalDialog() {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = dialogRef.current; if (dialog && !dialog.open) dialog.showModal(); }, []);
  return dialogRef;
}

function asObject(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function asArray(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function stringValue(value: unknown): string { return typeof value === "string" ? value : ""; }
function cloneObject(value: Record<string, unknown>): Record<string, unknown> { return JSON.parse(JSON.stringify(value)) as Record<string, unknown>; }
function pretty(value: unknown): string { return JSON.stringify(value, null, 2); }
function removeKeys(object: Record<string, unknown>, keys: string[]) { for (const key of keys) delete object[key]; }
function setOptional(object: Record<string, unknown>, key: string, value: string) { if (value.trim()) object[key] = value.trim(); else delete object[key]; }
function tabLabel(tab: EditorTab) { return tab === "llm" ? "LLM" : tab === "presets" ? "Presets" : tab[0].toUpperCase() + tab.slice(1); }
function errorMessage(error: unknown) { return error instanceof Error ? error.message : "The request failed. No changes were applied."; }
