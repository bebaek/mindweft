import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExecutionAgentOptionItem, ThreadListItem } from "../api/client";
import { WorkspacePage } from "./WorkspacePage";

const agents: ExecutionAgentOptionItem[] = [
  { id: "shared:general", name: "general", display_name: "General" },
  { id: "user:coding", name: "coding", display_name: "Coding" },
];
const thread: ThreadListItem = {
  thread_id: "source", title: "Original conversation", agent_ref: "shared:general",
  status: "idle", message_count: 2, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};
const api = {
  getPublicConfig: vi.fn(), getExecutionOptions: vi.fn(), listThreads: vi.fn(),
  listMessages: vi.fn(), getThreadLineage: vi.fn(),
  listPendingPrivateValueConsents: vi.fn(), forkThread: vi.fn(),
  createThread: vi.fn(), addMessage: vi.fn(), streamRun: vi.fn(),
};
vi.mock("../auth/auth-context", () => ({ useAuth: () => ({ api, authentication: { mode: "development" } }) }));
beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value(this: HTMLDialogElement) { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value(this: HTMLDialogElement) { this.removeAttribute("open"); } });
  Element.prototype.scrollIntoView = vi.fn();
});
beforeEach(() => {
  vi.resetAllMocks();
  api.getPublicConfig.mockResolvedValue({ image_input: { enabled: false, allowed_mime_types: [] } });
  api.getExecutionOptions.mockResolvedValue({
    agents: { default: "shared:general", items: agents },
    skills: { items: [] }, capability_profiles: { items: [] },
    llm_profiles: { items: [], effective_default: { name: "default", image_input_allowed: false, document_input_allowed: false } },
  });
  api.listThreads.mockResolvedValue({ threads: [thread], total: 1, limit: 50, offset: 0 });
  api.listMessages.mockResolvedValue([
    { id: "user-1", thread_id: "source", role: "user", content: "Help me", created_at: thread.created_at },
    { id: "answer-1", thread_id: "source", role: "assistant", content: "Here is some context", created_at: thread.created_at },
  ]);
  api.getThreadLineage.mockResolvedValue({ thread, parent: null, children: [], siblings: [] });
  api.listPendingPrivateValueConsents.mockResolvedValue([]);
  api.forkThread.mockResolvedValue({ thread_id: "child", parent_thread_id: "source", fork_message_id: "answer-1" });
});
afterEach(cleanup);
async function setup(existing = true) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><WorkspacePage /></QueryClientProvider>);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Agent" })).not.toBeDisabled());
  if (existing) {
    fireEvent.click(await screen.findByRole("button", { name: /Original conversation/ }));
    await screen.findByText("Help me");
  }
  return screen.getByRole("textbox", { name: /Message/ });
}
function command(input: HTMLElement, text: string) {
  fireEvent.change(input, { target: { value: text } });
  fireEvent.submit(input.closest("form")!);
}
function expectNoRun() {
  expect(api.addMessage).not.toHaveBeenCalled();
  expect(api.streamRun).not.toHaveBeenCalled();
  expect(api.createThread).not.toHaveBeenCalled();
}

describe("/agent", () => {
  it("reports the current agent without switching or sending a message", async () => {
    const input = await setup();
    command(input, "/agent current");
    await screen.findByText("Current agent: shared:general.");
    expect(input).toHaveValue("");
    expect(api.forkThread).not.toHaveBeenCalled();
    expectNoRun();
  });
  it("confirms an agent-aware fork without sending the command or running", async () => {
    const input = await setup();
    command(input, "/agent coding");
    const dialog = await screen.findByRole("dialog", { name: "Continue with another agent" });
    expect(api.forkThread).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(api.forkThread).toHaveBeenCalledWith("source", "answer-1", "user:coding"));
    await waitFor(() => expect(input).toHaveValue(""));
    expectNoRun();
  });
  it("opens a picker and preserves the command on cancel", async () => {
    const input = await setup();
    command(input, "/agent");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(input).toHaveValue("/agent");
    expect(api.forkThread).not.toHaveBeenCalled();
    expectNoRun();
  });
  it("selects an agent for a new conversation without creating a thread", async () => {
    const input = await setup(false);
    command(input, "/agent user:coding");
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Agent" })).toHaveValue("user:coding"));
    expect(api.forkThread).not.toHaveBeenCalled();
    expectNoRun();
  });
  it("does not fork when the current agent is selected", async () => {
    const input = await setup();
    command(input, "/agent general");
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Continue" }));
    await screen.findByText("Already using General.");
    expect(api.forkThread).not.toHaveBeenCalled();
    expectNoRun();
  });
  it("preserves the command and source when the fork fails", async () => {
    api.forkThread.mockRejectedValue(new Error("Target unavailable"));
    const input = await setup();
    command(input, "/agent coding");
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Continue" }));
    await screen.findByText("Target unavailable");
    expect(input).toHaveValue("/agent coding");
    expect(screen.getByRole("combobox", { name: "Agent" })).toHaveValue("shared:general");
    expectNoRun();
  });
  it("preserves an ordinary draft when switching via the selector", async () => {
    const input = await setup();
    fireEvent.change(input, { target: { value: "Please review this" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Agent" }), { target: { value: "user:coding" } });
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(api.forkThread).toHaveBeenCalled());
    expect(input).toHaveValue("Please review this");
    expectNoRun();
  });
  it("requires a scoped reference for ambiguous names", async () => {
    const input = await setup();
    // Both personal and shared agents can have the same friendly name.
    agents.push({ id: "shared:coding", name: "coding", display_name: "Coding" });
    try {
      command(input, "/agent coding");
      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
      fireEvent.click(within(dialog).getByRole("button", { name: "Coding (user:coding)" }));
      fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
      await waitFor(() => expect(api.forkThread).toHaveBeenCalledWith("source", "answer-1", "user:coding"));
    } finally { agents.pop(); }
  });
  it("blocks switching a remotely running thread", async () => {
    api.listThreads.mockResolvedValue({ threads: [{ ...thread, status: "running" }], total: 1, limit: 50, offset: 0 });
    const input = await setup();
    command(input, "/agent coding");
    await screen.findByText("Stop or wait for the current run before switching agents.");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.forkThread).not.toHaveBeenCalled();
    expectNoRun();
  });
  it("selects without forking a stored empty thread", async () => {
    api.listMessages.mockResolvedValue([]);
    const input = await setup(false);
    fireEvent.click(await screen.findByRole("button", { name: /Original conversation/ }));
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled());
    command(input, "/agent coding");
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Continue" }));
    await screen.findByText("Selected Coding for the next message.");
    expect(api.forkThread).not.toHaveBeenCalled();
    expectNoRun();
  });
  it("can switch at an earlier message", async () => {
    await setup();
    fireEvent.click(screen.getAllByRole("button", { name: "Continue with another agent…" })[0]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Coding (user:coding)" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(api.forkThread).toHaveBeenCalledWith("source", "user-1", "user:coding"));
    expectNoRun();
  });
  it("rejects an unknown name without sending it to the model", async () => {
    const input = await setup();
    command(input, "/agent missing");
    expect(within(await screen.findByRole("dialog")).getByRole("button", { name: "Continue" })).toBeDisabled();
    expectNoRun();
  });
});
