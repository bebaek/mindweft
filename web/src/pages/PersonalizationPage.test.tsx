import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { MinigentApiClient } from "../api/client";
import { AuthContext, type AuthContextValue } from "../auth/auth-context";
import { PersonalizationPage } from "./PersonalizationPage";

afterEach(cleanup);

it("validates personal configuration and stores a write-only MCP credential", async () => {
  const validateConfig = vi.fn().mockResolvedValue({ valid: true, errors: [], normalized_config: {} });
  const storeCredential = vi.fn().mockResolvedValue({
    tenant_id: "tenant-1",
    user_id: "user-1",
    credential_ref: "api:linear",
    header_name: "Authorization",
    version: 1,
    created_at: "2026-08-04T00:00:00Z",
    updated_at: "2026-08-04T00:00:00Z",
  });
  const api = {
    getUserExecutionConfig: vi.fn().mockResolvedValue({
      tenant_id: "tenant-1",
      user_id: "user-1",
      config: { mcp_servers: { items: [] } },
      version: 3,
      created_at: "2026-08-04T00:00:00Z",
      updated_at: "2026-08-04T00:00:00Z",
    }),
    getExecutionOptions: vi.fn().mockResolvedValue({
      tenant_id: "tenant-1",
      skills: { items: [] },
      capability_profiles: { items: [] },
      llm_profiles: { items: [{ name: "claude", display_name: "Claude Sonnet" }] },
      agents: { default: "user:personal-assistant", items: [] },
    }),
    validateUserExecutionConfig: validateConfig,
    updateUserExecutionConfig: vi.fn(),
    deleteUserExecutionConfig: vi.fn(),
    listUserResources: vi.fn().mockImplementation((type: string) => Promise.resolve(type === "agents"
      ? { items: [{ id: "user:personal-assistant", name: "Personal assistant", skill_refs: [], capability_profile_ref: null, llm_profile: "claude" }], version: 3 }
      : { items: [], version: 3 })),
    updateUserResource: vi.fn(),
    deleteUserResource: vi.fn(),
    listUserExecutionCredentials: vi.fn().mockResolvedValue({ items: [] }),
    getUserMCPAccess: vi.fn().mockResolvedValue({
      tenant_id: "tenant-1",
      user_id: "user-1",
      endpoint_path: "/user-mcp",
      personal_mcp_servers_allowed: true,
      personal_servers: [],
      shared_servers: [],
    }),
    getUserMCPStatus: vi.fn().mockResolvedValue({
      tenant_id: "tenant-1",
      user_id: "user-1",
      endpoint_path: "/user-mcp",
      execution_configured: true,
      execution_config_version: 3,
      encrypted_credentials_available: true,
      personal_mcp_servers_allowed: true,
      skills: 0,
      mcp_servers: 0,
      capability_profiles: 0,
      agents: 0,
      findings: [],
    }),
    updateUserExecutionCredential: storeCredential,
    deleteUserExecutionCredential: vi.fn(),
  } as unknown as MinigentApiClient;
  const auth: AuthContextValue = {
    authentication: { mode: "session" },
    api,
    session: { loading: false, enabled: true, authenticated: true, principal: { user_id: "user-1", tenant_id: "tenant-1", is_admin: false }, error: null },
    setAuthentication: vi.fn(),
    login: vi.fn(),
    completePasswordSetup: vi.fn(),
    logout: vi.fn(),
    refreshSession: vi.fn(),
  };
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}><PersonalizationPage /></AuthContext.Provider>
    </QueryClientProvider>,
  );

  expect(await screen.findByText("Version 3")).toBeInTheDocument();
  expect(await screen.findByRole("heading", { name: "User MCP access" })).toBeInTheDocument();
  expect(await screen.findByText("Default")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Protected" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Use as template" }));
  expect(screen.getByLabelText("Agent name")).toHaveValue("Personal assistant copy");
  expect(screen.getByLabelText("Agent model profile")).toHaveValue("claude");
  fireEvent.click(screen.getByRole("button", { name: "Add agent" }));
  await waitFor(() => expect(api.updateUserResource).toHaveBeenCalledWith(
    "agents",
    "user:personal-assistant-copy",
    expect.objectContaining({ llm_profile: "claude" }),
    3,
  ));
  expect(screen.getByText("http://localhost:3000/user-mcp")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Validate" }));
  await waitFor(() => expect(validateConfig).toHaveBeenCalledWith({ mcp_servers: { items: [] } }));
  expect(await screen.findByText("Configuration is valid")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Credential reference"), { target: { value: "api:linear" } });
  fireEvent.change(screen.getByLabelText("Secret header value"), { target: { value: "Bearer secret-token" } });
  fireEvent.click(screen.getByRole("button", { name: "Store credential" }));

  await waitFor(() => expect(storeCredential).toHaveBeenCalledWith("api:linear", {
    header_name: "Authorization",
    header_value: "Bearer secret-token",
    expected_version: undefined,
  }));
});
