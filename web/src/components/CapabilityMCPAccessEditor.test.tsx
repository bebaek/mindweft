import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CapabilityMCPAccessEditor } from "./CapabilityMCPAccessEditor";

afterEach(cleanup);

describe("CapabilityMCPAccessEditor", () => {
  it("updates an explicit MCP allowlist with checkboxes", () => {
    const onChange = vi.fn();
    render(
      <CapabilityMCPAccessEditor
        capabilities={JSON.stringify({
          default_profile: "safe-default",
          items: [{ name: "safe-default", mcp_server_names: [] }],
        })}
        mcpServers={JSON.stringify([
          { name: "home-assistant", url: "https://ha.example/mcp" },
          { name: "web-search", url: "https://search.example/mcp" },
        ])}
        onChange={onChange}
      />,
    );

    expect(screen.getByText("0 of 2 servers allowed")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("home-assistant"));

    const firstOutput: unknown = onChange.mock.calls[0]?.[0];
    expect(typeof firstOutput).toBe("string");
    const updated = JSON.parse(firstOutput as string) as {
      items: Array<{ mcp_server_names: string[] }>;
    };
    expect(updated.items[0].mcp_server_names).toEqual(["home-assistant"]);
  });

  it("shows inherited access distinctly and can clear it", () => {
    const onChange = vi.fn();
    render(
      <CapabilityMCPAccessEditor
        capabilities={JSON.stringify({ items: [{ name: "default" }] })}
        mcpServers={JSON.stringify([{ name: "docs" }])}
        onChange={onChange}
      />,
    );

    expect(screen.getByText("All tenant servers inherited")).toBeInTheDocument();
    expect(screen.getByLabelText("docs")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));

    const secondOutput: unknown = onChange.mock.calls[0]?.[0];
    expect(typeof secondOutput).toBe("string");
    const updated = JSON.parse(secondOutput as string) as {
      items: Array<{ mcp_server_names: string[] }>;
    };
    expect(updated.items[0].mcp_server_names).toEqual([]);
  });
});
