import type { ExecutionLlmOptionItem, ExecutionOptionsResponse, ThreadListItem } from "../api/client";

export interface LlmSelection {
  option?: ExecutionLlmOptionItem;
  name?: string;
  source: "conversation" | "override" | "agent" | "default";
  unavailable: boolean;
}

// Do not substitute the default for a missing explicit or agent-selected profile.
// This mirrors server precedence; it does not grant access or test credentials.
export function resolveLlmSelection(
  options: ExecutionOptionsResponse | undefined,
  thread: ThreadListItem | undefined,
  override: string,
  agentRef: string,
): LlmSelection {
  const agent = options?.agents.items.find((item) => (item.id ?? item.name) === agentRef || item.name === agentRef);
  const source = thread ? (thread.llm_profile ? "conversation" : "default") : override ? "override" : agent?.llm_profile ? "agent" : "default";
  const requested = thread ? thread.llm_profile : override || agent?.llm_profile;
  const name = requested?.replace(/^shared:/, "") || undefined;
  const option = name
    ? options?.llm_profiles.items.find((item) => item.name === name)
    : options?.llm_profiles.effective_default;
  return { option, name: name ?? option?.name, source, unavailable: Boolean(options && name && !option) };
}

export function llmOptionLabel(option: ExecutionLlmOptionItem): string {
  return [option.display_name ?? option.name, option.provider, option.model].filter(Boolean).join(" · ");
}
