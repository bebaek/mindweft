import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { MinigentApiClient } from "../api/client";
import { AuthContext, type AuthContextValue } from "../auth/auth-context";
import { OAuthImportPanel } from "./OAuthImportPanel";

afterEach(cleanup);

it("extracts only Pi's openai-codex credential before upload", async () => {
  const credential = {
    type: "oauth",
    access: "openai-access",
    refresh: "openai-refresh",
    expires: 1_900_000_000_000,
    accountId: "account-1",
  };
  const importCredential = vi.fn().mockResolvedValue({
    tenant_id: "tenant-1",
    provider_id: "openai-codex",
    source: "pi",
    connected: true,
    account_id: "account-1",
    expires_at: "2030-03-17T17:46:40Z",
  });
  const api = {
    getTenantOpenAIOAuthCredential: vi.fn().mockResolvedValue({
      tenant_id: "tenant-1",
      provider_id: "openai-codex",
      source: "pi",
      connected: false,
    }),
    importTenantOpenAIOAuthFromPi: importCredential,
    deleteTenantOpenAIOAuthCredential: vi.fn(),
  } as unknown as MinigentApiClient;
  const auth: AuthContextValue = {
    authentication: { mode: "session" },
    api,
    session: { loading: false, enabled: true, authenticated: true, principal: { user_id: "owner", tenant_id: "tenant-1", is_admin: false }, error: null },
    setAuthentication: vi.fn(),
    login: vi.fn(),
    completePasswordSetup: vi.fn(),
    logout: vi.fn(),
    refreshSession: vi.fn(),
  };
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}><OAuthImportPanel tenantId="tenant-1" /></AuthContext.Provider>
    </QueryClientProvider>,
  );

  await screen.findByText(/Locate/);
  fireEvent.click(screen.getByRole("checkbox"));
  const authJson = JSON.stringify({
    "openai-codex": credential,
    anthropic: { type: "oauth", access: "must-not-upload" },
  });
  const file = new File([authJson], "auth.json", { type: "application/json" });
  Object.defineProperty(file, "text", { value: () => Promise.resolve(authJson) });
  fireEvent.change(screen.getByLabelText("Pi auth.json"), { target: { files: [file] } });

  await waitFor(() => expect(importCredential).toHaveBeenCalledWith("tenant-1", credential));
  expect(importCredential.mock.calls[0][1]).not.toHaveProperty("anthropic");
});
