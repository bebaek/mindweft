import {
  INTERNAL_MCP_SERVER_PRESETS,
  parseMcpServers,
  serverName,
  toggleMcpServerPreset,
} from "./mcpServerPresets";

export function MCPServerPicker({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const parsed = parseMcpServers(value);
  const invalid = parsed === null;

  return <section className="mcp-server-picker" aria-labelledby="internal-mcp-title">
    <div className="mcp-server-picker-heading">
      <div><strong id="internal-mcp-title">Internal services</strong><span>Enable trusted tools already hosted alongside Minigent.</span></div>
      <span className="mcp-server-picker-badge">Quick add</span>
    </div>
    <div className="mcp-server-preset-list">
      {INTERNAL_MCP_SERVER_PRESETS.map((preset) => {
        const enabled = parsed?.some((server) => serverName(server) === preset.name) ?? false;
        return <article className="mcp-server-preset" key={preset.name}>
          <div><strong>{preset.title}</strong><p>{preset.description}</p><small>{preset.detail}</small></div>
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
