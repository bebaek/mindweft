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
