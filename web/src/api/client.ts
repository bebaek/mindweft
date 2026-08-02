export type Authentication =
  | { mode: "session" }
  | { mode: "development"; tenantId: string; userId: string; isAdmin: boolean }
  | { mode: "bearer"; token: string };

export interface HealthResponse {
  status: "ok";
}

export interface ReadinessResponse {
  status: "ready" | "not_ready";
  checks: Record<string, "ok" | "failed">;
}

export interface ExecutionOptionItem {
  name: string;
  description?: string | null;
}

export interface ExecutionOptionSection {
  default?: string | null;
  items: ExecutionOptionItem[];
}

export interface ExecutionOptionsResponse {
  tenant_id: string;
  skills: ExecutionOptionSection;
  capability_profiles: ExecutionOptionSection;
  llm_profiles: ExecutionOptionSection;
}

export type ThreadStatus = "idle" | "running" | "error";

export interface ThreadListItem {
  thread_id: string;
  title: string;
  status: ThreadStatus;
  skill_name?: string | null;
  capability_profile?: string | null;
  llm_profile?: string | null;
  message_count: number;
  created_at: string;
  updated_at: string;
}

export interface ThreadListResponse {
  threads: ThreadListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface Message {
  id: string;
  thread_id: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  created_at: string;
}

export interface RunEvent {
  type: string;
  content?: string;
  detail?: string;
  status_code?: number;
  [key: string]: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly details: unknown;

  constructor(message: string, status: number, details: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

export class MinigentApiClient {
  readonly #baseUrl: string;
  readonly #authentication: Authentication;

  constructor(authentication: Authentication, baseUrl = "") {
    this.#authentication = authentication;
    this.#baseUrl = baseUrl.replace(/\/$/, "");
  }

  getHealth(signal?: AbortSignal): Promise<HealthResponse> {
    return this.#request<HealthResponse>("/health", { signal });
  }

  getReadiness(signal?: AbortSignal): Promise<ReadinessResponse> {
    return this.#request<ReadinessResponse>("/health/ready", { signal });
  }

  getExecutionOptions(signal?: AbortSignal): Promise<ExecutionOptionsResponse> {
    return this.#request<ExecutionOptionsResponse>("/execution-options", { signal });
  }

  listThreads(limit = 50, signal?: AbortSignal): Promise<ThreadListResponse> {
    return this.#request<ThreadListResponse>(`/threads?limit=${String(limit)}`, { signal });
  }

  createThread(signal?: AbortSignal): Promise<{ thread_id: string }> {
    return this.#request<{ thread_id: string }>("/threads", {
      method: "POST",
      body: JSON.stringify({}),
      signal,
    });
  }

  listMessages(threadId: string, signal?: AbortSignal): Promise<Message[]> {
    return this.#request<Message[]>(`/threads/${encodeURIComponent(threadId)}/messages`, { signal });
  }

  addMessage(threadId: string, content: string, signal?: AbortSignal): Promise<Message> {
    return this.#request<Message>(`/threads/${encodeURIComponent(threadId)}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
      signal,
    });
  }

  cancelRun(threadId: string): Promise<unknown> {
    return this.#request(`/threads/${encodeURIComponent(threadId)}/run/cancel`, {
      method: "POST",
    });
  }

  deleteThread(threadId: string): Promise<void> {
    return this.#request<void>(`/threads/${encodeURIComponent(threadId)}`, {
      method: "DELETE",
    });
  }

  async streamRun(
    threadId: string,
    onEvent: (event: RunEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const headers = this.#headers();
    headers.set("Accept", "application/x-ndjson");
    const response = await fetch(
      `${this.#baseUrl}/threads/${encodeURIComponent(threadId)}/run/stream`,
      { method: "POST", headers, credentials: "include", signal },
    );
    if (!response.ok) {
      const details = await readResponseBody(response);
      throw new ApiError(errorMessage(details, response.status), response.status, details);
    }
    if (!response.body) throw new Error("Streaming responses are not supported by this browser");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) emitNdjsonLine(line, onEvent);
      if (done) break;
    }
    emitNdjsonLine(buffer, onEvent);
  }

  #headers(init?: HeadersInit): Headers {
    const headers = new Headers(init);
    headers.set("Accept", "application/json");
    if (this.#authentication.mode === "bearer") {
      headers.set("Authorization", `Bearer ${this.#authentication.token}`);
    } else if (this.#authentication.mode === "development") {
      headers.set("X-Minigent-Tenant-Id", this.#authentication.tenantId);
      headers.set("X-Minigent-User-Id", this.#authentication.userId);
      if (this.#authentication.isAdmin) headers.set("X-Minigent-Admin", "true");
    }
    return headers;
  }

  async #request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = this.#headers(init.headers);
    if (init.body !== undefined) headers.set("Content-Type", "application/json");

    const response = await fetch(`${this.#baseUrl}${path}`, {
      ...init,
      headers,
      credentials: "include",
    });

    if (!response.ok) {
      const details = await readResponseBody(response);
      throw new ApiError(errorMessage(details, response.status), response.status, details);
    }

    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }
}

async function readResponseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return (await response.json()) as unknown;
  }
  return await response.text();
}

function errorMessage(details: unknown, status: number): string {
  if (typeof details === "object" && details !== null && "detail" in details) {
    const detail = details.detail;
    if (typeof detail === "string") return detail;
  }
  if (typeof details === "string" && details.trim()) return details;
  return `Request failed with status ${String(status)}`;
}

function emitNdjsonLine(line: string, onEvent: (event: RunEvent) => void): void {
  const trimmed = line.trim();
  if (trimmed) onEvent(JSON.parse(trimmed) as RunEvent);
}
