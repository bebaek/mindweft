import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { MinigentApiClient } from "../api/client";
import { AuthContext, type AuthContextValue } from "../auth/auth-context";
import { ExecutionConfigPanel } from "./ExecutionConfigPanel";

afterEach(cleanup);

it("loads the platform-admin execution scope without a tenant", async () => {
  const getAdminExecutionConfig = vi.fn().mockResolvedValue({
    version: 2,
    config: { llm: { provider: "openai", model: "gpt-test" } },
  });
  const getAdminTenantExecutionConfig = vi.fn();
  const api = {
    getAdminExecutionConfig,
    getAdminTenantExecutionConfig,
    getAdminDeploymentMcpServerCatalog: vi.fn().mockResolvedValue({
      items: [],
      managed: false,
      allow_custom_mcp_servers: true,
    }),
  } as unknown as MinigentApiClient;
  const auth: AuthContextValue = {
    authentication: { mode: "session" },
    api,
    session: {
      loading: false,
      enabled: true,
      authenticated: true,
      principal: { user_id: "admin", tenant_id: "admin-scope", is_admin: true },
      error: null,
    },
    setAuthentication: vi.fn(),
    login: vi.fn(),
    completePasswordSetup: vi.fn(),
    logout: vi.fn(),
    refreshSession: vi.fn(),
  };
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <ExecutionConfigPanel platform />
      </AuthContext.Provider>
    </QueryClientProvider>,
  );

  expect(await screen.findByRole("heading", { name: "Platform admin execution" })).toBeVisible();
  expect(await screen.findByText("gpt-test")).toBeVisible();
  await waitFor(() => expect(getAdminExecutionConfig).toHaveBeenCalled());
  expect(getAdminTenantExecutionConfig).not.toHaveBeenCalled();
});
