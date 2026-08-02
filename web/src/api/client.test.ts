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
