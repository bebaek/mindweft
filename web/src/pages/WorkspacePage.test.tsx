import { describe, expect, it } from "vitest";
import type { Message } from "../api/client";
import { persistedToolSteps, visibleChatMessages, withDefaultAgent } from "./workspaceMessages";

function message(id: string, role: Message["role"], content: string): Message {
  return {
    id,
    thread_id: "thread-1",
    role,
    content,
    created_at: "2026-01-01T00:00:00Z",
  };
}

describe("withDefaultAgent", () => {
  it("preserves personal configuration while replacing either default-agent key style", () => {
    expect(withDefaultAgent({
      defaults: { agentRef: "user:old", skill_refs: ["user:research"] },
      agents: { items: [{ id: "user:researcher" }] },
    }, "user:researcher")).toEqual({
      defaults: { agent_ref: "user:researcher", skill_refs: ["user:research"] },
      agents: { items: [{ id: "user:researcher" }] },
    });
  });

  it("creates defaults for a user without saved execution configuration", () => {
    expect(withDefaultAgent(undefined, "shared:general")).toEqual({
      defaults: { agent_ref: "shared:general" },
    });
  });
});

describe("persistedToolSteps", () => {
  it("pairs historical tool calls and results by call id", () => {
    const user = message("user-1", "user", "Check the thermostat");
    const call = { ...message("call-1", "assistant", ""), tool_name: "get_state", tool_call_id: "tool-1", tool_arguments: { entity_id: "climate.thermostat" } };
    const result = { ...message("result-1", "tool", '{"state":"cool"}'), tool_name: "get_state", tool_call_id: "tool-1" };
    const assistant = message("assistant-1", "assistant", "The thermostat is online.");

    expect(persistedToolSteps([user, call, result, assistant], assistant.id)).toEqual([
      { toolCall: call, result, status: "success" },
    ]);
  });

  it("keeps unmatched historical calls pending and detects errors", () => {
    const user = message("user-1", "user", "Check devices");
    const failedCall = { ...message("call-1", "assistant", ""), tool_name: "get_state", tool_call_id: "failed" };
    const failedResult = { ...message("result-1", "tool", '{"error":"Unavailable"}'), tool_call_id: "failed" };
    const pendingCall = { ...message("call-2", "assistant", ""), tool_name: "get_state", tool_call_id: "pending" };
    const assistant = message("assistant-1", "assistant", "I could not check every device.");

    expect(persistedToolSteps([user, failedCall, failedResult, pendingCall, assistant], assistant.id).map((step) => step.status)).toEqual(["error", "pending"]);
  });
});

describe("visibleChatMessages", () => {
  it("hides the persisted copy while the same final reply is still streamed", () => {
    const user = message("user-1", "user", "Prepare the release");
    const assistant = message("assistant-1", "assistant", "Deployment ready");

    expect(visibleChatMessages([user, assistant], "Deployment ready")).toEqual([user]);
  });

  it("keeps history when the streamed reply does not duplicate the latest assistant message", () => {
    const assistant = message("assistant-1", "assistant", "Earlier response");

    expect(visibleChatMessages([assistant], "Current response")).toEqual([assistant]);
    expect(visibleChatMessages([assistant], null)).toEqual([assistant]);
  });

  it("hides empty assistant tool-call records from the transcript", () => {
    const user = message("user-1", "user", "Check the thermostat");
    const toolCall = { ...message("assistant-tool-1", "assistant", ""), tool_name: "get_state", tool_call_id: "call-1" };
    const assistant = message("assistant-1", "assistant", "The thermostat is online.");

    expect(visibleChatMessages([user, toolCall, assistant], null)).toEqual([user, assistant]);
  });

  it("continues to exclude system and tool messages", () => {
    const user = message("user-1", "user", "Hello");
    const system = message("system-1", "system", "System context");
    const tool = message("tool-1", "tool", "Tool result");

    expect(visibleChatMessages([system, user, tool], null)).toEqual([user]);
  });
});
