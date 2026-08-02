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

  async #request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");

    if (this.#authentication.mode === "bearer") {
      headers.set("Authorization", `Bearer ${this.#authentication.token}`);
    } else if (this.#authentication.mode === "development") {
      headers.set("X-Minigent-Tenant-Id", this.#authentication.tenantId);
      headers.set("X-Minigent-User-Id", this.#authentication.userId);
      if (this.#authentication.isAdmin) {
        headers.set("X-Minigent-Admin", "true");
      }
    }

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
