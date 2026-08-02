import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { AuthProvider } from "./auth/AuthProvider";

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (path.endsWith("/auth/session")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ enabled: false, authenticated: false, principal: null }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }
      if (path.endsWith("/health/ready")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ status: "ready", checks: { thread_store: "ok" } }),
            {
              status: 200,
              headers: { "content-type": "application/json" },
            },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ detail: "Authentication required" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        }),
      );
    }),
  );
});

it("signs in with configured static credentials", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (path.endsWith("/auth/session") && init?.method === "POST") {
        return Promise.resolve(new Response(JSON.stringify({ enabled: true, authenticated: true, principal: { user_id: "admin", tenant_id: "platform", is_admin: true } }), { status: 200, headers: { "content-type": "application/json" } }));
      }
      if (path.endsWith("/auth/session")) {
        return Promise.resolve(new Response(JSON.stringify({ enabled: true, authenticated: false, principal: null }), { status: 200, headers: { "content-type": "application/json" } }));
      }
      if (path.endsWith("/health/ready")) {
        return Promise.resolve(new Response(JSON.stringify({ status: "ready", checks: {} }), { status: 200, headers: { "content-type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ detail: "Not found" }), { status: 404, headers: { "content-type": "application/json" } }));
    }),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><AuthProvider><App /></AuthProvider></QueryClientProvider>);

  expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: "admin" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByText("Build, observe, and govern your agents.")).toBeInTheDocument();
  expect(screen.getByText("admin")).toBeInTheDocument();
});

it("renders the production console shell and readiness status", async () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider><App /></AuthProvider>
    </QueryClientProvider>,
  );

  expect(await screen.findByText("Build, observe, and govern your agents.")).toBeInTheDocument();
  expect(await screen.findByText("Ready")).toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "Primary navigation" })).toBeInTheDocument();
});
