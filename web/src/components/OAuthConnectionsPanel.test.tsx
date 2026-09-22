import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { OAuthConnectionsPanel } from "./OAuthConnectionsPanel";
import { OAuthProfileBindings } from "./OAuthProfileBindings";

const api = {
  listOAuthConnections: vi.fn().mockResolvedValue({ items: [
    { name: "work", ref: "shared:work", provider_id: "openai-codex", connected: true, expired: false },
    { name: "personal", ref: "shared:personal", provider_id: "openai-codex", connected: false },
  ] }),
  disconnectOAuthConnection: vi.fn().mockResolvedValue(undefined),
};
vi.mock("../auth/auth-context", () => ({ useAuth: () => ({ api, authentication: { mode: "session" } }) }));
afterEach(cleanup);
function wrapper(children: React.ReactNode) { return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>; }

test("disconnects only the selected account after explicit confirmation", async () => {
  render(wrapper(<OAuthConnectionsPanel />));
  fireEvent.click(await screen.findByRole("button", { name: "Disconnect work" }));
  expect(api.disconnectOAuthConnection).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm disconnect" }));
  await waitFor(() => expect(api.disconnectOAuthConnection).toHaveBeenCalledWith("work"));
  expect(screen.getByText(/Status reflects stored credentials only/)).toBeInTheDocument();
});
test("binds a profile without modifying another profile or its model", async () => {
  const changed = vi.fn();
  render(wrapper(<OAuthProfileBindings value={JSON.stringify({ coder: { provider: "generic-oauth", model: "model-a" }, other: { provider: "mock" } })} onChange={changed} />));
  const select = await screen.findByLabelText("OAuth connection for coder");
  await waitFor(() => expect(select).not.toBeDisabled());
  fireEvent.change(select, { target: { value: "shared:personal" } });
  expect(changed).toHaveBeenCalledWith(JSON.stringify({ coder: { provider: "generic-oauth", model: "model-a", oauth_connection_ref: "shared:personal" }, other: { provider: "mock" } }, null, 2));
});
