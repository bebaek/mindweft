import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentPresetEditor } from "./AgentPresetEditor";

afterEach(cleanup);

describe("AgentPresetEditor", () => {
  const props = {
    agents: JSON.stringify({ items: [{ name: "default", skill_names: ["coding"], capability_profile: "safe", llm_profile: "fast" }] }),
    capabilities: JSON.stringify({ items: [{ name: "safe" }, { name: "full" }] }),
    llmProfiles: JSON.stringify({ fast: {}, accurate: {} }),
  };

  it("creates a preset using profile selectors", () => {
    const onChange = vi.fn();
    render(<AgentPresetEditor {...props} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "New preset" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Release assistant" } });
    fireEvent.change(screen.getByPlaceholderText("coding, reviewer"), { target: { value: "release, checks" } });
    fireEvent.change(screen.getByLabelText("Capability profile"), { target: { value: "full" } });
    fireEvent.change(screen.getByLabelText("Model profile"), { target: { value: "accurate" } });
    fireEvent.click(screen.getByRole("button", { name: "Add preset" }));

    const updated = JSON.parse(onChange.mock.calls[0]?.[0] as string) as { items: Array<Record<string, unknown>> };
    expect(updated.items[1]).toMatchObject({
      name: "Release assistant",
      skill_names: ["release", "checks"],
      capability_profile: "full",
      llm_profile: "accurate",
    });
  });

  it("loads supported camelCase and singular skill aliases", () => {
    const onChange = vi.fn();
    render(
      <AgentPresetEditor
        {...props}
        agents={JSON.stringify({ items: [{ name: "legacy", skillName: "coding", capabilityProfile: "safe", llmProfile: "fast" }] })}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("textbox", { name: /^Skills/ })).toHaveValue("coding");
    expect(screen.getByLabelText("Capability profile")).toHaveValue("safe");
    expect(screen.getByLabelText("Model profile")).toHaveValue("fast");
    fireEvent.click(screen.getByRole("button", { name: "Save preset" }));

    const updated = JSON.parse(onChange.mock.calls[0]?.[0] as string) as { items: Array<Record<string, unknown>> };
    expect(updated.items[0]).toMatchObject({
      skill_names: ["coding"],
      capability_profile: "safe",
      llm_profile: "fast",
    });
  });

  it("edits and removes an existing preset", () => {
    const onChange = vi.fn();
    render(<AgentPresetEditor {...props} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("textbox", { name: /^Skills/ })).toHaveValue("coding");
    expect(screen.getByLabelText("Capability profile")).toHaveValue("safe");
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Runs release checks" } });
    fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
    const edited = JSON.parse(onChange.mock.calls[0]?.[0] as string) as { items: Array<Record<string, unknown>> };
    expect(edited.items[0]).toMatchObject({
      description: "Runs release checks",
      skill_names: ["coding"],
      capability_profile: "safe",
      llm_profile: "fast",
    });

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    const removed = JSON.parse(onChange.mock.calls[1]?.[0] as string) as { items: unknown[] };
    expect(removed.items).toEqual([]);
  });
});
