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
