import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentPresetEditor } from "./AgentPresetEditor";

afterEach(cleanup);

describe("AgentPresetEditor", () => {
  const props = {
    agents: JSON.stringify({ items: [{ name: "default", skill_refs: ["shared:coding"], capability_profile_ref: "safe", llm_profile: "fast" }] }),
    capabilities: JSON.stringify({ items: [{ name: "safe" }, { name: "full" }] }),
    llmProfiles: JSON.stringify({ fast: {}, accurate: {} }),
  };

  it("creates a preset using profile selectors", () => {
    const onChange = vi.fn();
    render(<AgentPresetEditor {...props} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "New preset" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Release assistant" } });
    fireEvent.change(screen.getByPlaceholderText("shared:coding, user:reviewer"), { target: { value: "shared:release, user:checks" } });
    fireEvent.change(screen.getByLabelText("Capability profile"), { target: { value: "full" } });
    fireEvent.change(screen.getByLabelText("Model profile"), { target: { value: "accurate" } });
    fireEvent.click(screen.getByRole("button", { name: "Add preset" }));

    const updated = JSON.parse(onChange.mock.calls[0]?.[0] as string) as { items: Array<Record<string, unknown>> };
    expect(updated.items[1]).toMatchObject({
      name: "Release assistant",
      skill_refs: ["shared:release", "user:checks"],
      capability_profile_ref: "full",
      llm_profile: "accurate",
    });
  });

  it("edits and removes an existing preset", () => {
    const onChange = vi.fn();
    render(<AgentPresetEditor {...props} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Runs release checks" } });
    fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
    const edited = JSON.parse(onChange.mock.calls[0]?.[0] as string) as { items: Array<Record<string, unknown>> };
    expect(edited.items[0].description).toBe("Runs release checks");

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    const removed = JSON.parse(onChange.mock.calls[1]?.[0] as string) as { items: unknown[] };
    expect(removed.items).toEqual([]);
  });
});
