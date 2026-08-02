import { afterEach, describe, expect, it, vi } from "vitest";
import { MinigentApiClient } from "./client";

afterEach(() => vi.restoreAllMocks());

describe("MinigentApiClient", () => {
  it("uses secure same-origin credentials by default", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await new MinigentApiClient({ mode: "session" }).getHealth();

    expect(fetchMock).toHaveBeenCalledWith(
      "/health",
      expect.objectContaining({ credentials: "include" }),
    );
    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(headers.has("Authorization")).toBe(false);
    expect(headers.has("X-Minigent-User-Id")).toBe(false);
  });

  it("creates and clears same-origin sessions", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) =>
      Promise.resolve(
        new Response(
          init?.method === "DELETE"
            ? null
            : JSON.stringify({ enabled: true, authenticated: true, principal: { user_id: "admin", tenant_id: "platform", is_admin: true } }),
          { status: init?.method === "DELETE" ? 204 : 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    const client = new MinigentApiClient({ mode: "session" });

    await client.login("admin", "secret");
    await client.logout();

    expect(fetchMock.mock.calls[0][0]).toBe("/auth/session");
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({ method: "POST", credentials: "include", body: JSON.stringify({ username: "admin", password: "secret" }) }));
    expect(fetchMock.mock.calls[1][1]).toEqual(expect.objectContaining({ method: "DELETE", credentials: "include" }));
  });

  it("adds explicit development principal headers", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await new MinigentApiClient({
      mode: "development",
      tenantId: "tenant-1",
      userId: "user-1",
      isAdmin: true,
    }).getHealth();

    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(headers.get("X-Minigent-Tenant-Id")).toBe("tenant-1");
    expect(headers.get("X-Minigent-User-Id")).toBe("user-1");
    expect(headers.get("X-Minigent-Admin")).toBe("true");
  });

  it("parses NDJSON run events across stream chunks", async () => {
    const encoder = new TextEncoder();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode('{"type":"run.started"}\n{"type":"assistant.'));
            controller.enqueue(encoder.encode('message","content":"Done"}\n'));
            controller.close();
          },
        }),
        { status: 200, headers: { "content-type": "application/x-ndjson" } },
      ),
    );
    const events: string[] = [];

    await new MinigentApiClient({ mode: "session" }).streamRun(
      "thread/1",
      (event) => events.push(event.type),
    );

    expect(events).toEqual(["run.started", "assistant.message"]);
  });

  it("uploads binary attachments with authentication and MIME headers", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          attachment_id: "attachment-1",
          thread_id: "thread-1",
          mime_type: "image/png",
          size_bytes: 3,
          created_at: "2026-01-01T00:00:00Z",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const file = new File([new Uint8Array([1, 2, 3])], "image.png", { type: "image/png" });

    await new MinigentApiClient({
      mode: "development",
      tenantId: "tenant-1",
      userId: "user-1",
      isAdmin: false,
    }).uploadAttachment("thread-1", file);

    const request = fetchMock.mock.calls[0];
    const headers = new Headers(request[1]?.headers);
    expect(request[0]).toBe("/threads/thread-1/attachments/binary");
    expect(request[1]?.body).toBe(file);
    expect(headers.get("content-type")).toBe("image/png");
    expect(headers.get("x-minigent-tenant-id")).toBe("tenant-1");
  });

  it("encodes tenant thread filters and prune previews", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({ tenant_id: "tenant/1", threads: [], total: 0, limit: 10, offset: 0 }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      ),
    );
    const client = new MinigentApiClient({ mode: "session" });

    await client.getAdminTenantThreads("tenant/1", {
      limit: 10,
      offset: 20,
      status: "error",
      skill: "code review",
    });
    await client.pruneAdminTenantThreads("tenant/1", {
      updated_before: "2026-01-01T00:00:00Z",
      profile: "safe/default",
      dry_run: true,
    });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/admin/tenants/tenant%2F1/threads?limit=10&offset=20&status=error&skill=code+review",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/admin/tenants/tenant%2F1/threads/prune?updated_before=2026-01-01T00%3A00%3A00Z&profile=safe%2Fdefault&dry_run=true",
    );
    expect(fetchMock.mock.calls[1][1]?.method).toBe("POST");
  });

  it("imports only the selected Pi OAuth credential", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ tenant_id: "tenant/1", provider_id: "openai-codex", source: "pi", connected: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const credential = { type: "oauth", access: "access", refresh: "refresh", expires: 1, accountId: "account" };

    await new MinigentApiClient({ mode: "session" }).importTenantOpenAIOAuthFromPi("tenant/1", credential);

    expect(fetchMock.mock.calls[0][0]).toBe("/admin/tenants/tenant%2F1/oauth/openai-codex/import/pi");
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ credential, acknowledge_transfer: true }),
    }));
  });

  it("returns structured API failures", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Admin access required" }), {
        status: 403,
        headers: { "content-type": "application/json" },
      }),
    );

    const request = new MinigentApiClient({ mode: "session" }).getExecutionOptions();

    await expect(request).rejects.toEqual(
      expect.objectContaining({ status: 403, message: "Admin access required" }),
    );
  });
});
