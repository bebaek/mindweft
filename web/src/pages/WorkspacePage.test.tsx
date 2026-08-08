import { describe, expect, it } from "vitest";
import type { Message } from "../api/client";
import { visibleChatMessages } from "./workspaceMessages";

function message(id: string, role: Message["role"], content: string): Message {
  return {
    id,
    thread_id: "thread-1",
    role,
    content,
    created_at: "2026-01-01T00:00:00Z",
  };
}

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

  it("continues to exclude system and tool messages", () => {
    const user = message("user-1", "user", "Hello");
    const system = message("system-1", "system", "System context");
    const tool = message("tool-1", "tool", "Tool result");

    expect(visibleChatMessages([system, user, tool], null)).toEqual([user]);
  });
});
