import { afterEach, beforeEach, expect, test, vi } from "vitest";

const metadata = { name: "preview", launch_id: "a".repeat(32), version: "test", auth_mode: "local-credential" };

beforeEach(() => {
  vi.resetModules();
  window.history.replaceState(null, "", "/console/");
  window.sessionStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

test("ordinary deployments keep existing authentication without a downgrade", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 404 }));
  vi.stubGlobal("fetch", fetchMock);
  const connection = await import("./localConnection");
  await connection.bootstrapLocalConnection();
  expect(connection.localInstance()).toBeNull();
  expect(connection.localHeaders()).toEqual({});
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("removes the ticket before requests and exchanges it once", async () => {
  window.history.replaceState(null, "", "/console/#local_ticket=one-time-ticket");
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    expect(window.location.hash).toBe("");
    return Promise.resolve(Response.json(url === "/local-instance" ? metadata : { session_key: "session-proof" }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const connection = await import("./localConnection");
  await connection.bootstrapLocalConnection();
  expect(fetchMock).toHaveBeenCalledTimes(2);
  const exchangeOptions = fetchMock.mock.calls[1][1] as RequestInit;
  expect(new Headers(exchangeOptions.headers).get("X-Mindweft-Browser-Ticket")).toBe("one-time-ticket");
  expect(connection.localHeaders()).toEqual({
    "X-Mindweft-Launch-Id": metadata.launch_id, "X-Mindweft-Session-Key": "session-proof",
  });
  expect(JSON.stringify(window.sessionStorage)).not.toContain("one-time-ticket");
});

test("a new launch does not reuse the previous session proof", async () => {
  window.sessionStorage.setItem(`mindweft-local-session:preview:${"b".repeat(32)}`, "old-proof");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(metadata)));
  const connection = await import("./localConnection");
  await connection.bootstrapLocalConnection();
  expect(connection.localHeaders()["X-Mindweft-Session-Key"]).toBe("");
});

test("failed ticket exchange stops bootstrap rather than switching authentication", async () => {
  window.history.replaceState(null, "", "/console/#local_ticket=expired");
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(metadata))
    .mockResolvedValueOnce(new Response(null, { status: 401 }));
  vi.stubGlobal("fetch", fetchMock);
  const connection = await import("./localConnection");
  await expect(connection.bootstrapLocalConnection()).rejects.toThrow("Reopen with mindweft instances open preview");
  expect(window.location.hash).toBe("");
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("stale requests signal disconnection and do not follow redirects", async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(metadata))
    .mockResolvedValueOnce(new Response(null, { status: 409 }));
  vi.stubGlobal("fetch", fetchMock);
  const connection = await import("./localConnection");
  await connection.bootstrapLocalConnection();
  const listener = vi.fn();
  window.addEventListener("mindweft-local-disconnected", listener);
  try {
    await connection.connectionFetch("/threads");
    expect(listener).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/threads", expect.objectContaining({ redirect: "error" }));
  } finally {
    window.removeEventListener("mindweft-local-disconnected", listener);
  }
});
