import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { AuthProvider } from "./auth/AuthProvider";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
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

it("renders the production console shell and readiness status", async () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider><App /></AuthProvider>
    </QueryClientProvider>,
  );

  expect(screen.getByText("Build, observe, and govern your agents.")).toBeInTheDocument();
  expect(await screen.findByText("Ready")).toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "Primary navigation" })).toBeInTheDocument();
});
