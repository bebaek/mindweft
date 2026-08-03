import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MCPServerPicker } from "./MCPServerPicker";
import {
  INTERNAL_MCP_SERVER_PRESETS,
  toggleMcpServerPreset,
} from "./mcpServerPresets";

afterEach(cleanup);

describe("MCPServerPicker", () => {
  it("quick-adds the internal web search service without replacing custom servers", () => {
    const existing = JSON.stringify([{ name: "custom", url: "https://mcp.example.com" }]);
    const changed = toggleMcpServerPreset(existing, INTERNAL_MCP_SERVER_PRESETS[0], true);

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
      INTERNAL_MCP_SERVER_PRESETS[0].server,
      { name: "custom", url: "https://mcp.example.com" },
    ]);

    expect(JSON.parse(toggleMcpServerPreset(value, INTERNAL_MCP_SERVER_PRESETS[0], false))).toEqual([
      { name: "custom", url: "https://mcp.example.com" },
    ]);
  });

  it("shows an enabled state and emits the updated JSON", () => {
    const onChange = vi.fn<(value: string) => void>();
    render(<MCPServerPicker value="[]" onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Enable" }));

    const emitted = onChange.mock.calls[0]?.[0];
    expect(emitted).toBeDefined();
    expect(JSON.parse(emitted ?? "null")).toEqual([
      INTERNAL_MCP_SERVER_PRESETS[0].server,
    ]);
  });

  it("does not overwrite invalid advanced JSON", () => {
    const onChange = vi.fn<(value: string) => void>();
    render(<MCPServerPicker value="not json" onChange={onChange} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Fix the advanced MCP JSON");
    expect(screen.getByRole("button", { name: "Enable" })).toBeDisabled();
  });
});
