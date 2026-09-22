import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { UserResourceEditors } from "./UserResourceEditors";

const api = {
  updateUserResource: vi.fn().mockResolvedValue({ version: 8 }),
  listUserResources: vi.fn().mockResolvedValue({ items: [], version: 0 }),
  getExecutionOptions: vi.fn().mockResolvedValue({
    agents: { default: "coding", items: [{ name: "coding", source: "shared", skills: ["coding-workspace"], capability_profile: "coding" }] },
    llm_profiles: { items: [] },
  }),
};
vi.mock("../auth/auth-context", () => ({ useAuth: () => ({ api, authentication: { mode: "session" } }) }));
afterEach(cleanup);

test("seeds a personal coding copy without modifying the built-in", async () => {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><UserResourceEditors /></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Customize coding" }));
  expect(screen.getByLabelText("Agent name")).toHaveValue("My coding");
  expect(screen.getByLabelText(/Skill references/)).toHaveValue("shared:coding-workspace");
  expect(screen.getByLabelText("Capability profile reference")).toHaveValue("shared:coding");
});


test("edits an existing agent model without replacing its identity or description", async () => {
  api.listUserResources.mockImplementation((kind: string) => Promise.resolve({ version: 7, items: kind === "agents" ? [{ id: "user:reviewer", name: "Reviewer", description: "Keep this", skill_refs: [], llm_profile: "deep" }] : [] }));
  api.getExecutionOptions.mockResolvedValue({ agents: { items: [] }, llm_profiles: { items: [{ name: "deep", provider: "mock", model: "deep-model" }, { name: "fast" }] } });
  render(<QueryClientProvider client={new QueryClient()}><UserResourceEditors /></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
  expect(screen.getByLabelText("Agent model profile")).toHaveValue("deep");
  expect(screen.getByRole("option", { name: "deep · mock · deep-model" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Agent name"), { target: { value: "Renamed reviewer" } });
  fireEvent.change(screen.getByLabelText("Agent model profile"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Save agent" }));
  await waitFor(() => expect(api.updateUserResource).toHaveBeenCalledWith("agents", "user:reviewer", expect.objectContaining({ id: "user:reviewer", name: "Renamed reviewer", description: "Keep this", llm_profile: null }), 7));
});

test("does not silently replace a removed model profile when editing", async () => {
  api.updateUserResource.mockClear();
  api.listUserResources.mockImplementation((kind: string) => Promise.resolve({ version: 7, items: kind === "agents" ? [{ id: "user:reviewer", name: "Reviewer", skill_refs: [], llm_profile: "removed" }] : [] }));
  api.getExecutionOptions.mockResolvedValue({ agents: { items: [] }, llm_profiles: { items: [] } });
  render(<QueryClientProvider client={new QueryClient()}><UserResourceEditors /></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
  expect(screen.getByRole("option", { name: "removed (unavailable)" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save agent" }));
  await screen.findByText("Choose an available model profile or inherit the default.");
  expect(api.updateUserResource).not.toHaveBeenCalled();
});
