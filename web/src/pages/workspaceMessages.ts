import type { Message } from "../api/client";

export function visibleChatMessages(messages: Message[] | undefined, streamedReply: string | null): Message[] {
  const visible = messages?.filter((message) => message.role === "user" || message.role === "assistant") ?? [];
  const lastMessage = visible.at(-1);
  if (streamedReply !== null && lastMessage?.role === "assistant" && lastMessage.content === streamedReply) {
    return visible.slice(0, -1);
  }
  return visible;
}
