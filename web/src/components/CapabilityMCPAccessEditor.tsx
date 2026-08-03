import { useMemo } from "react";

type JsonObject = Record<string, unknown>;

export function CapabilityMCPAccessEditor({
  capabilities,
  mcpServers,
  onChange,
}: {
  capabilities: string;
  mcpServers: string;
  onChange: (value: string) => void;
}) {
  const parsed = useMemo(
    () => parseEditorState(capabilities, mcpServers),
    [capabilities, mcpServers],
  );

  if (parsed.error) {
    return <section className="capability-mcp-editor"><p className="mcp-server-picker-error" role="alert">{parsed.error}</p></section>;
  }
  if (parsed.profiles.length === 0) {
    return <section className="capability-mcp-editor"><p className="mcp-server-picker-status">Add a capability profile in the JSON below to configure its MCP access.</p></section>;
  }

  return (
    <section className="capability-mcp-editor" aria-labelledby="capability-mcp-title">
      <div className="mcp-server-picker-heading">
        <div>
          <strong id="capability-mcp-title">MCP access by capability</strong>
          <span>Choose which configured MCP servers each capability profile can expose.</span>
        </div>
        <span className="mcp-server-picker-badge">Access</span>
      </div>
      {parsed.serverNames.length === 0 && <p className="mcp-server-picker-status">Enable tenant MCP servers in the Tools tab first.</p>}
      <div className="capability-mcp-list">
        {parsed.profiles.map((profile) => {
          const effective = profile.explicitNames ?? parsed.serverNames;
          return (
            <article className="capability-mcp-profile" key={profile.name}>
              <header>
                <div><strong>{profile.name}</strong><small>{profile.explicitNames === null ? "All tenant servers inherited" : `${String(effective.length)} of ${String(parsed.serverNames.length)} servers allowed`}</small></div>
                <div>
                  <button type="button" onClick={() => onChange(updateProfile(capabilities, profile.index, parsed.serverNames))}>Allow all</button>
                  <button type="button" onClick={() => onChange(updateProfile(capabilities, profile.index, []))}>Clear all</button>
                </div>
              </header>
              <div className="capability-mcp-options">
                {parsed.serverNames.map((name) => (
                  <label key={name}>
                    <input
                      type="checkbox"
                      checked={effective.includes(name)}
                      onChange={(event) => {
                        const current = new Set(effective);
                        if (event.target.checked) current.add(name);
                        else current.delete(name);
                        onChange(updateProfile(capabilities, profile.index, parsed.serverNames.filter((item) => current.has(item))));
                      }}
                    />
                    <span>{name}</span>
                  </label>
                ))}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function parseEditorState(capabilities: string, mcpServers: string): {
  error: string | null;
  profiles: Array<{ index: number; name: string; explicitNames: string[] | null }>;
  serverNames: string[];
} {
  try {
    const capabilityConfig = JSON.parse(capabilities) as unknown;
    const serverConfig = JSON.parse(mcpServers) as unknown;
    if (!isObject(capabilityConfig) || !Array.isArray(capabilityConfig.items)) {
      return { error: "Capability profiles JSON must contain an items array.", profiles: [], serverNames: [] };
    }
    if (!Array.isArray(serverConfig)) {
      return { error: "MCP servers JSON must be an array.", profiles: [], serverNames: [] };
    }
    const rawProfiles: unknown[] = capabilityConfig.items;
    const rawServers: unknown[] = serverConfig;
    const serverNames: string[] = [];
    for (const server of rawServers) {
      if (isObject(server) && typeof server.name === "string" && !serverNames.includes(server.name)) {
        serverNames.push(server.name);
      }
    }
    const profiles: Array<{ index: number; name: string; explicitNames: string[] | null }> = [];
    rawProfiles.forEach((profile, index) => {
      if (!isObject(profile) || typeof profile.name !== "string") return;
      const rawNames = profile.mcp_server_names ?? profile.mcpServerNames;
      profiles.push({
        index,
        name: profile.name,
        explicitNames: Array.isArray(rawNames)
          ? (rawNames as unknown[]).filter((name): name is string => typeof name === "string")
          : null,
      });
    });
    return { error: null, profiles, serverNames };
  } catch {
    return { error: "Fix the capability profile and MCP server JSON before editing access.", profiles: [], serverNames: [] };
  }
}

function updateProfile(capabilities: string, index: number, names: string[]): string {
  const parsed = JSON.parse(capabilities) as JsonObject;
  const items: unknown[] = Array.isArray(parsed.items) ? Array.from(parsed.items as unknown[]) : [];
  const profile = isObject(items[index]) ? { ...items[index] } : {};
  profile.mcp_server_names = names;
  delete profile.mcpServerNames;
  items[index] = profile;
  parsed.items = items;
  return JSON.stringify(parsed, null, 2);
}

function isObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
