import type { AdminMcpServerCatalogItem } from "../api/client";
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
}: {
  value: string;
  catalog: AdminMcpServerCatalogItem[];
  pending: boolean;
  error: string | null;
  onChange: (value: string) => void;
}) {
  const parsed = parseMcpServers(value);
  const invalid = parsed === null;

  return <section className="mcp-server-picker" aria-labelledby="internal-mcp-title">
    <div className="mcp-server-picker-heading">
      <div><strong id="internal-mcp-title">Internal services</strong><span>Enable tools advertised by this Minigent deployment.</span></div>
      <span className="mcp-server-picker-badge">Quick add</span>
    </div>
    {pending && <p className="mcp-server-picker-status">Loading available services…</p>}
    {error && <p className="mcp-server-picker-error" role="alert">{error}</p>}
    {!pending && !error && catalog.length === 0 && <p className="mcp-server-picker-status">No internal services are configured for this deployment.</p>}
    <div className="mcp-server-preset-list">
      {catalog.map((preset) => {
        const presetName = serverName(preset.server);
        const enabled = parsed?.some((server) => serverName(server) === presetName) ?? false;
        return <article className="mcp-server-preset" key={preset.id}>
          <div><strong>{preset.title}</strong><p>{preset.description}</p>{preset.detail && <small>{preset.detail}</small>}</div>
          <button
            type="button"
            className={enabled ? "enabled" : ""}
            disabled={invalid}
            aria-pressed={enabled}
            onClick={() => onChange(toggleMcpServerPreset(value, preset, !enabled))}
          >{enabled ? "Enabled — remove" : "Enable"}</button>
        </article>;
      })}
    </div>
    {invalid && <p className="mcp-server-picker-error" role="alert">Fix the advanced MCP JSON before changing internal services.</p>}
  </section>;
}
