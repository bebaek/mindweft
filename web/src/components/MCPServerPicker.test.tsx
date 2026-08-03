import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminMcpServerCatalogItem } from "../api/client";
import { MCPServerPicker } from "./MCPServerPicker";
import { toggleMcpServerPreset } from "./mcpServerPresets";

afterEach(cleanup);

const WEB_SEARCH_PRESET: AdminMcpServerCatalogItem = {
  id: "web-search",
  title: "Web search",
  description: "Search the web, current news, and relevance-ranked page content.",
  detail: "Local Brave Search sidecar · 3 tools",
  server: {
    name: "web-search",
    url: "http://127.0.0.1:8766/mcp",
    headers: {},
    allowed_tools: ["brave_web_search", "brave_news_search", "brave_llm_context"],
  },
};

describe("MCPServerPicker", () => {
  it("quick-adds the internal web search service without replacing custom servers", () => {
    const existing = JSON.stringify([{ name: "custom", url: "https://mcp.example.com" }]);
    const changed = toggleMcpServerPreset(existing, WEB_SEARCH_PRESET, true);

    expect(JSON.parse(changed)).toEqual([
      { name: "custom", url: "https://mcp.example.com" },
      {
        name: "web-search",
        url: "http://127.0.0.1:8766/mcp",
        headers: {},
        allowed_tools: ["brave_web_search", "brave_news_search", "brave_llm_context"],
      },
    ]);
  });

  it("removes only the selected internal service", () => {
    const value = JSON.stringify([
      WEB_SEARCH_PRESET.server,
      { name: "custom", url: "https://mcp.example.com" },
    ]);

    expect(JSON.parse(toggleMcpServerPreset(value, WEB_SEARCH_PRESET, false))).toEqual([
      { name: "custom", url: "https://mcp.example.com" },
    ]);
  });

  it("shows an enabled state and emits the updated JSON", () => {
    const onChange = vi.fn<(value: string) => void>();
    render(<MCPServerPicker value="[]" catalog={[WEB_SEARCH_PRESET]} pending={false} error={null} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Enable" }));

    const emitted = onChange.mock.calls[0]?.[0];
    expect(emitted).toBeDefined();
    expect(JSON.parse(emitted ?? "null")).toEqual([
      WEB_SEARCH_PRESET.server,
    ]);
  });

  it("does not overwrite invalid advanced JSON", () => {
    const onChange = vi.fn<(value: string) => void>();
    render(<MCPServerPicker value="not json" catalog={[WEB_SEARCH_PRESET]} pending={false} error={null} onChange={onChange} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Fix the advanced MCP JSON");
    expect(screen.getByRole("button", { name: "Enable" })).toBeDisabled();
  });
});
