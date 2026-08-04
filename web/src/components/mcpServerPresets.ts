import type { AdminMcpServerCatalogItem } from "../api/client";

export function toggleMcpServerPreset(
  value: string,
  preset: AdminMcpServerCatalogItem,
  enabled: boolean,
): string {
  const servers = parseMcpServers(value);
  if (servers === null) return value;
  const presetName = serverName(preset.server);
  const withoutPreset = servers.filter((server) => serverName(server) !== presetName);
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
