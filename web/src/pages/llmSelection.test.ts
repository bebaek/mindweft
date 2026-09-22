import { describe, expect, it } from "vitest";
import type { ExecutionOptionsResponse, ThreadListItem } from "../api/client";
import { resolveLlmSelection } from "./llmSelection";

const fast = { name: "fast", image_input_allowed: false, document_input_allowed: false, capability_declared: true };
const deep = { ...fast, name: "deep" };
const options: ExecutionOptionsResponse = {
  tenant_id: "tenant-1",
  skills: { items: [] }, capability_profiles: { items: [] },
  agents: { items: [{ id: "user:reviewer", name: "Reviewer", llm_profile: "deep" }] },
  llm_profiles: { default: "fast", effective_default: fast, items: [fast, deep] },
};
const thread = (profile: string | null) => ({ thread_id: "t", llm_profile: profile } as ThreadListItem);

describe("model preference resolution", () => {
  it("prioritizes persisted conversation, explicit override, agent, then default", () => {
    expect(resolveLlmSelection(options, thread("fast"), "deep", "user:reviewer")).toMatchObject({ option: fast, source: "conversation" });
    expect(resolveLlmSelection(options, undefined, "fast", "user:reviewer")).toMatchObject({ option: fast, source: "override" });
    expect(resolveLlmSelection(options, undefined, "", "user:reviewer")).toMatchObject({ option: deep, source: "agent" });
    expect(resolveLlmSelection(options, undefined, "", "")).toMatchObject({ option: fast, source: "default" });
    expect(resolveLlmSelection(options, thread(null), "deep", "user:reviewer").option).toEqual(fast);
  });
  it("does not substitute the default for a missing profile", () => {
    expect(resolveLlmSelection(options, thread("removed"), "", "")).toMatchObject({ option: undefined, unavailable: true });
    expect(resolveLlmSelection(options, undefined, "removed", "user:reviewer")).toMatchObject({ option: undefined, unavailable: true });
    expect(resolveLlmSelection({ ...options, llm_profiles: { ...options.llm_profiles, items: [fast] } }, undefined, "", "user:reviewer")).toMatchObject({ option: undefined, unavailable: true });
  });
  it("does not label profiles unavailable while loading", () => {
    expect(resolveLlmSelection(undefined, undefined, "deep", "").unavailable).toBe(false);
  });
});
