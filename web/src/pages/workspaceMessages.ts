import type { Message } from "../api/client";

export function withDefaultAgent(
  config: Record<string, unknown> | undefined,
  agentRef: string,
): Record<string, unknown> {
  const next = { ...(config ?? {}) };
  const existingDefaults = next.defaults;
  const defaults = existingDefaults && typeof existingDefaults === "object" && !Array.isArray(existingDefaults)
    ? { ...existingDefaults as Record<string, unknown> }
    : {};
  delete defaults.agent_ref;
  delete defaults.agentRef;
  next.defaults = { ...defaults, agent_ref: agentRef };
  return next;
}

export function reasoningSummary(metadata: Record<string, unknown> | null | undefined): string | null {
  if (!metadata) return null;
  const direct = metadata.reasoning_content;
  if (typeof direct === "string" && direct.trim()) return direct.trim();
  const items = metadata.generic_oauth_responses_output_items;
  if (!Array.isArray(items)) return null;
  const summaries: string[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object" || (item as Record<string, unknown>).type !== "reasoning") continue;
    const summary = (item as Record<string, unknown>).summary;
    if (!Array.isArray(summary)) continue;
    for (const part of summary) {
      if (!part || typeof part !== "object") continue;
      const text = (part as Record<string, unknown>).text;
      if (typeof text === "string" && text.trim()) summaries.push(text.trim());
    }
  }
  return summaries.length > 0 ? summaries.join("\n\n") : null;
}

export interface PersistedToolStep {
  toolCall: Message;
  result?: Message;
  status: "pending" | "success" | "error";
}

export function persistedToolSteps(
  messages: Message[] | undefined,
  assistantMessageId: string,
): PersistedToolStep[] {
  if (!messages) return [];
  const assistantIndex = messages.findIndex((message) => message.id === assistantMessageId);
  if (assistantIndex < 0) return [];

  const calls = new Map<string, Message>();
  const results = new Map<string, Message>();
  for (let index = assistantIndex - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "user") break;
    if (message.role === "assistant" && message.content.trim()) break;
    if (message.role === "assistant" && message.tool_call_id) {
      calls.set(message.tool_call_id, message);
    } else if (message.role === "tool" && message.tool_call_id) {
      results.set(message.tool_call_id, message);
    }
  }
  return [...calls.entries()].reverse().map(([toolCallId, toolCall]) => {
    const result = results.get(toolCallId);
    return {
      toolCall,
      ...(result ? { result } : {}),
      status: !result ? "pending" : result.content.includes('"error"') ? "error" : "success",
    };
  });
}

export function visibleChatMessages(messages: Message[] | undefined, streamedReply: string | null): Message[] {
  // Tool-call records are stored as empty assistant messages so the runtime can
  // replay them to the model. They belong in run activity, not the transcript.
  const visible = messages?.filter((message) =>
    message.role === "user" || (message.role === "assistant" && message.content.trim().length > 0)
  ) ?? [];
  const lastMessage = visible.at(-1);
  if (streamedReply !== null && lastMessage?.role === "assistant" && lastMessage.content === streamedReply) {
    return visible.slice(0, -1);
  }
  return visible;
}
