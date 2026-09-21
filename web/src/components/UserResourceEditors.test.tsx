import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { UserResourceEditors } from "./UserResourceEditors";

const api = {
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
