export interface LocalInstance {
  name: string;
  launch_id: string;
  version: string;
  auth_mode: "static-tokens";
  session_handoff: true;
  workspace_access?: "coding" | "read-only";
}

let instance: LocalInstance | null = null;
let sessionKey = "";

export function localInstance(): LocalInstance | null { return instance; }

// Called once before React mounts (including StrictMode). Never infer auth from a 401.
export async function bootstrapLocalConnection(): Promise<void> {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const ticket = fragment.get("local_ticket");
  if (ticket !== null) {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  const response = await fetch("/local-instance", { cache: "no-store", credentials: "omit", redirect: "error" });
  if (response.status === 404) return;
  if (!response.ok) throw new Error("Unable to identify this server. Reload to retry.");
  const data = await response.json() as Partial<LocalInstance>;
  if (data.auth_mode !== "static-tokens" || data.session_handoff !== true) return;
  if (typeof data.name !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(data.name)
      || typeof data.launch_id !== "string" || !/^[0-9a-f]{32}$/.test(data.launch_id)
      || typeof data.version !== "string") throw new Error("Invalid local instance metadata.");
  instance = data as LocalInstance;
  const storageKey = `mindweft-local-session:${instance.name}:${instance.launch_id}`;
  try { sessionKey = window.sessionStorage.getItem(storageKey) ?? ""; } catch { /* optional */ }
  if (ticket !== null) {
    const exchange = await fetch("/auth/session/exchange", {
      method: "POST", credentials: "same-origin", redirect: "error",
      headers: { "X-Mindweft-Browser-Ticket": ticket, "X-Mindweft-Launch-Id": instance.launch_id },
    });
    if (!exchange.ok) throw new Error("Browser ticket expired or was already used. Reopen with mindweft instances open " + instance.name);
    const result = await exchange.json() as { session_key: string };
    sessionKey = result.session_key;
    try { window.sessionStorage.setItem(storageKey, sessionKey); } catch { /* memory-only session */ }
  }
}

export function localHeaders(): Record<string, string> {
  return instance ? { "X-Mindweft-Launch-Id": instance.launch_id, "X-Mindweft-Session-Key": sessionKey } : {};
}

export async function connectionFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, instance ? { ...init, redirect: "error" } : init);
  if (instance && (response.status === 401 || response.status === 409)) {
    window.dispatchEvent(new Event("mindweft-local-disconnected"));
  }
  return response;
}
