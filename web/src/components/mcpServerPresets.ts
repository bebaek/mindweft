export interface MCPServerPreset {
  name: string;
  title: string;
  description: string;
  detail: string;
  server: Record<string, unknown>;
}

export const INTERNAL_MCP_SERVER_PRESETS: MCPServerPreset[] = [
  {
    name: "web-search",
    title: "Web search",
    description: "Search the web, current news, and relevance-ranked page content.",
    detail: "Local Brave Search sidecar · 3 tools",
    server: {
      name: "web-search",
      url: "http://127.0.0.1:8766/mcp",
      headers: {},
      allowed_tools: ["brave_web_search", "brave_news_search", "brave_llm_context"],
    },
  },
];

export function toggleMcpServerPreset(value: string, preset: MCPServerPreset, enabled: boolean): string {
  const servers = parseMcpServers(value);
  if (servers === null) return value;
  const withoutPreset = servers.filter((server) => serverName(server) !== preset.name);
  return JSON.stringify(enabled ? [...withoutPreset, preset.server] : withoutPreset, null, 2);
}

export function parseMcpServers(value: string): unknown[] | null {
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function serverName(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const name = (value as Record<string, unknown>).name;
  return typeof name === "string" ? name : "";
}
