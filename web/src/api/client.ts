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

export interface ImagePart {
  type: "image";
  mime_type: string;
  attachment_id: string;
  detail: "auto" | "low" | "high";
}

export interface TextPart {
  type: "text";
  text: string;
}

export type MessagePart = TextPart | ImagePart;

export interface Message {
  id: string;
  thread_id: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  parts?: MessagePart[] | null;
  created_by?: string | null;
  metadata?: Record<string, unknown> | null;
  tool_name?: string | null;
  tool_call_id?: string | null;
  tool_arguments?: Record<string, unknown> | null;
  created_at: string;
}

export interface ImageInputConfig {
  enabled: boolean;
  max_bytes: number;
  max_images: number;
  max_total_bytes: number;
  max_pixels: number;
  max_dimension: number;
  allowed_mime_types: string[];
}

export interface PublicConfig {
  image_input: ImageInputConfig;
  [key: string]: unknown;
}

export interface AttachmentMetadata {
  attachment_id: string;
  thread_id: string;
  mime_type: string;
  size_bytes: number;
  created_by?: string | null;
  created_at: string;
}

export interface ThreadContextUsage {
  estimated: boolean;
  total_tokens: number;
  summary_tokens: number;
  message_tokens: number;
  message_count: number;
  summarized_message_count: number;
  unsummarized_message_count: number;
}

export interface RawThreadContext {
  thread_id: string;
  summary: string;
  summarized_message_count: number;
  messages: Message[];
  rendered: string;
  usage: ThreadContextUsage;
}

export interface CompactThreadResponse {
  thread_id: string;
  summary: string;
  compacted_message_count: number;
  message_count: number;
  usage_before: ThreadContextUsage;
  usage: ThreadContextUsage;
}

export type TenantStatus = "provisioning" | "active" | "suspended" | "archived" | "deleted";
export type TenantUserRole = "owner" | "admin" | "member" | "viewer";
export type TenantUserStatus = "invited" | "active" | "suspended" | "deleted";

export interface AdminTenantInput {
  id?: string;
  slug: string;
  name: string;
  status?: TenantStatus;
  plan?: string | null;
  region?: string | null;
  metadata?: Record<string, unknown>;
}

export interface AdminTenantPatch {
  slug?: string;
  name?: string;
  plan?: string | null;
  region?: string | null;
  metadata?: Record<string, unknown>;
}

export interface AdminTenantUserInput {
  user_id: string;
  email?: string | null;
  display_name?: string | null;
  role: TenantUserRole;
  status: TenantUserStatus;
  metadata?: Record<string, unknown>;
}

export interface AdminTenantUserPatch {
  email?: string | null;
  display_name?: string | null;
  role?: TenantUserRole;
  status?: TenantUserStatus;
  metadata?: Record<string, unknown>;
}

export type EntitlementLimitValue = number | string | boolean | null;

export interface AdminTenantEntitlementsInput {
  features: Record<string, boolean>;
  limits: Record<string, EntitlementLimitValue>;
}

export interface AdminTenantEntitlements extends AdminTenantEntitlementsInput {
  tenant_id: string;
  version: number;
  updated_at: string;
}

export interface AdminTenantEntitlementsValidation {
  valid: boolean;
  features: { ok: boolean; errors: string[] };
  limits: { ok: boolean; errors: string[] };
}

export interface AdminThreadSummary {
  thread_id: string;
  tenant_id: string;
  status: ThreadStatus;
  created_at: string;
  updated_at: string;
  skill_name?: string | null;
  skill_names?: string[] | null;
  capability_profile?: string | null;
  message_count: number;
}

export interface AdminThreadList {
  tenant_id: string;
  threads: AdminThreadSummary[];
  limit: number;
  offset: number;
  total: number;
  next_offset?: number | null;
}

export interface AdminThreadDetail extends AdminThreadSummary {
  context: {
    summary: string;
    summarized_message_count: number;
    updated_at: string;
  };
  messages: Message[];
}

export interface AdminThreadFilters {
  limit?: number;
  offset?: number;
  status?: ThreadStatus | "";
  profile?: string;
  skill?: string;
  created_after?: string;
  updated_after?: string;
}

export interface AdminThreadDeleteResult {
  deleted: boolean;
  tenant_id: string;
  thread_id: string;
}

export interface AdminThreadPruneInput {
  updated_before: string;
  status?: ThreadStatus | "";
  profile?: string;
  skill?: string;
  dry_run?: boolean;
}

export interface AdminThreadPruneResult {
  tenant_id: string;
  deleted_count: number;
  updated_before: string;
  dry_run: boolean;
  candidate_thread_ids: string[];
}

export interface AdminAuditRecord {
  audit_id: string;
  tenant_id: string;
  actor_user_id: string;
  action: string;
  affected_count: number;
  thread_ids: string[];
  resource_type?: string | null;
  resource_id?: string | null;
  old_values?: Record<string, unknown> | null;
  new_values?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
}

export interface AdminAuditRecordList {
  tenant_id: string;
  audit_records: AdminAuditRecord[];
  limit: number;
  offset: number;
  total: number;
  next_offset?: number | null;
}

export interface AdminAuditFilters {
  limit?: number;
  offset?: number;
  action?: string;
  actor?: string;
  created_after?: string;
  created_before?: string;
}

export interface AdminTenantExecutionConfig {
  tenant_id: string;
  version: number;
  config: Record<string, unknown>;
}

export interface AdminExecutionValidationSection {
  ok: boolean;
  errors: string[];
}

export interface AdminMcpValidation {
  name: string;
  url: string;
  ok: boolean;
  error?: string | null;
  tool_count: number;
  protocol_version?: string | null;
  session: boolean;
  server_name?: string | null;
  server_version?: string | null;
}

export interface AdminExecutionValidation {
  valid: boolean;
  config_shape: AdminExecutionValidationSection;
  llm: AdminExecutionValidationSection & {
    provider?: string | null;
    model?: string | null;
    base_url?: string | null;
  };
  tools: AdminExecutionValidationSection & {
    local_tools: string[];
    unknown_local_tools: string[];
    mcp_servers: AdminMcpValidation[];
  };
}

export interface AdminTenant {
  id: string;
  slug: string;
  name: string;
  status: TenantStatus;
  plan?: string | null;
  region?: string | null;
  metadata: Record<string, unknown>;
  created_by?: string | null;
  updated_by?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminTenantListResponse {
  tenants: AdminTenant[];
  limit: number;
  offset: number;
  total: number;
  next_offset?: number | null;
}

export interface AdminTenantUser {
  id: string;
  tenant_id: string;
  user_id: string;
  email?: string | null;
  display_name?: string | null;
  role: TenantUserRole;
  status: TenantUserStatus;
  created_at: string;
  updated_at: string;
}

export interface AdminTenantUserListResponse {
  tenant_id: string;
  users: AdminTenantUser[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminTenantDomain {
  id: string;
  tenant_id: string;
  domain: string;
  verified: boolean;
  created_at: string;
}

export interface AdminAttachmentStatistics {
  tenant_id: string;
  total_count: number;
  total_bytes: number;
  pending_count: number;
  pending_bytes: number;
  referenced_count: number;
  referenced_bytes: number;
  max_count: number;
  max_bytes: number;
}

export interface AdminRunConcurrency {
  tenant_id: string;
  active_runs: number;
  active_users: number;
  tenant_capacity: number;
  user_capacity: number;
  next_expiration?: string | null;
}

export interface PrivateValueDisclosure {
  path: string;
  kind: string;
  count: number;
}

export interface PrivateValueConsentRequest {
  consent_id: string;
  thread_id: string;
  tool_name: string;
  argument_fingerprint: string;
  status: string;
  one_shot: boolean;
  expires_at: number;
  disclosures: PrivateValueDisclosure[];
}

export interface PrivateValueAction {
  consent_id: string;
  thread_id: string;
  tool_name: string;
  state: "pending" | "executing";
  expires_at: number;
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

  listAdminTenants(signal?: AbortSignal): Promise<AdminTenantListResponse> {
    return this.#request<AdminTenantListResponse>("/admin/tenants?limit=200", { signal });
  }

  createAdminTenant(input: AdminTenantInput): Promise<AdminTenant> {
    return this.#request<AdminTenant>("/admin/tenants", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  updateAdminTenant(tenantId: string, input: AdminTenantPatch): Promise<AdminTenant> {
    return this.#request<AdminTenant>(`/admin/tenants/${encodeURIComponent(tenantId)}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    });
  }

  listAdminTenantUsers(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminTenantUserListResponse> {
    return this.#request<AdminTenantUserListResponse>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/users?limit=200`,
      { signal },
    );
  }

  createAdminTenantUser(
    tenantId: string,
    input: AdminTenantUserInput,
  ): Promise<AdminTenantUser> {
    return this.#request<AdminTenantUser>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/users`,
      { method: "POST", body: JSON.stringify(input) },
    );
  }

  updateAdminTenantUser(
    tenantId: string,
    userRecordId: string,
    input: AdminTenantUserPatch,
  ): Promise<AdminTenantUser> {
    return this.#request<AdminTenantUser>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/users/${encodeURIComponent(userRecordId)}`,
      { method: "PATCH", body: JSON.stringify(input) },
    );
  }

  deleteAdminTenantUser(tenantId: string, userRecordId: string): Promise<void> {
    return this.#request<void>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/users/${encodeURIComponent(userRecordId)}`,
      { method: "DELETE" },
    );
  }

  async listAdminTenantDomains(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminTenantDomain[]> {
    const response = await this.#request<{ domains: AdminTenantDomain[] }>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/domains`,
      { signal },
    );
    return response.domains;
  }

  addAdminTenantDomain(tenantId: string, domain: string): Promise<AdminTenantDomain> {
    return this.#request<AdminTenantDomain>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/domains`,
      { method: "POST", body: JSON.stringify({ domain }) },
    );
  }

  verifyAdminTenantDomain(tenantId: string, domainId: string): Promise<AdminTenantDomain> {
    return this.#request<AdminTenantDomain>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/domains/${encodeURIComponent(domainId)}/verify`,
      { method: "POST" },
    );
  }

  deleteAdminTenantDomain(tenantId: string, domainId: string): Promise<void> {
    return this.#request<void>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/domains/${encodeURIComponent(domainId)}`,
      { method: "DELETE" },
    );
  }

  getAdminTenantEntitlements(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminTenantEntitlements> {
    return this.#request<AdminTenantEntitlements>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/entitlements`,
      { signal },
    );
  }

  validateAdminTenantEntitlements(
    tenantId: string,
    input: AdminTenantEntitlementsInput,
  ): Promise<AdminTenantEntitlementsValidation> {
    return this.#request<AdminTenantEntitlementsValidation>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/entitlements/validate`,
      { method: "POST", body: JSON.stringify(input) },
    );
  }

  updateAdminTenantEntitlements(
    tenantId: string,
    input: AdminTenantEntitlementsInput,
  ): Promise<AdminTenantEntitlements> {
    return this.#request<AdminTenantEntitlements>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/entitlements`,
      { method: "PUT", body: JSON.stringify(input) },
    );
  }

  deleteAdminTenantEntitlements(tenantId: string): Promise<void> {
    return this.#request<void>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/entitlements`,
      { method: "DELETE" },
    );
  }

  getAdminTenantThreads(
    tenantId: string,
    filters: AdminThreadFilters = {},
    signal?: AbortSignal,
  ): Promise<AdminThreadList> {
    return this.#request<AdminThreadList>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/threads${queryString(filters)}`,
      { signal },
    );
  }

  getAdminTenantThread(
    tenantId: string,
    threadId: string,
    signal?: AbortSignal,
  ): Promise<AdminThreadDetail> {
    return this.#request<AdminThreadDetail>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/threads/${encodeURIComponent(threadId)}`,
      { signal },
    );
  }

  deleteAdminTenantThread(tenantId: string, threadId: string): Promise<AdminThreadDeleteResult> {
    return this.#request<AdminThreadDeleteResult>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/threads/${encodeURIComponent(threadId)}`,
      { method: "DELETE" },
    );
  }

  pruneAdminTenantThreads(
    tenantId: string,
    input: AdminThreadPruneInput,
  ): Promise<AdminThreadPruneResult> {
    return this.#request<AdminThreadPruneResult>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/threads/prune${queryString(input)}`,
      { method: "POST" },
    );
  }

  getAdminTenantAuditRecords(
    tenantId: string,
    filters: AdminAuditFilters = {},
    signal?: AbortSignal,
  ): Promise<AdminAuditRecordList> {
    return this.#request<AdminAuditRecordList>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/audit-records${queryString(filters)}`,
      { signal },
    );
  }

  getAdminTenantExecutionConfig(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminTenantExecutionConfig> {
    return this.#request<AdminTenantExecutionConfig>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/execution-config`,
      { signal },
    );
  }

  validateAdminTenantExecutionConfig(
    tenantId: string,
    config: Record<string, unknown>,
  ): Promise<AdminExecutionValidation> {
    return this.#request<AdminExecutionValidation>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/execution-config/validate`,
      { method: "POST", body: JSON.stringify({ config }) },
    );
  }

  updateAdminTenantExecutionConfig(
    tenantId: string,
    config: Record<string, unknown>,
  ): Promise<AdminTenantExecutionConfig> {
    return this.#request<AdminTenantExecutionConfig>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/execution-config`,
      { method: "PUT", body: JSON.stringify({ config }) },
    );
  }

  deleteAdminTenantExecutionConfig(tenantId: string): Promise<void> {
    return this.#request<void>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/execution-config`,
      { method: "DELETE" },
    );
  }

  getAdminAttachmentStatistics(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminAttachmentStatistics> {
    return this.#request<AdminAttachmentStatistics>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/attachments/statistics`,
      { signal },
    );
  }

  getAdminRunConcurrency(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminRunConcurrency> {
    return this.#request<AdminRunConcurrency>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/run-concurrency`,
      { signal },
    );
  }

  transitionAdminTenant(
    tenantId: string,
    status: "active" | "suspended" | "archived",
  ): Promise<AdminTenant> {
    const action = status === "active" ? "activate" : status === "suspended" ? "suspend" : "archive";
    return this.#request<AdminTenant>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/${action}`,
      { method: "POST" },
    );
  }

  getPublicConfig(signal?: AbortSignal): Promise<PublicConfig> {
    return this.#request<PublicConfig>("/config", { signal });
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

  addMessage(
    threadId: string,
    content: string,
    parts?: MessagePart[],
    signal?: AbortSignal,
  ): Promise<Message> {
    return this.#request<Message>(`/threads/${encodeURIComponent(threadId)}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, ...(parts ? { parts } : {}) }),
      signal,
    });
  }

  async uploadAttachment(
    threadId: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<AttachmentMetadata> {
    const headers = this.#headers();
    headers.set("Content-Type", file.type);
    const response = await fetch(
      `${this.#baseUrl}/threads/${encodeURIComponent(threadId)}/attachments/binary`,
      { method: "POST", headers, body: file, credentials: "include", signal },
    );
    if (!response.ok) {
      const details = await readResponseBody(response);
      throw new ApiError(errorMessage(details, response.status), response.status, details);
    }
    return (await response.json()) as AttachmentMetadata;
  }

  async getAttachmentBlob(
    threadId: string,
    attachmentId: string,
    signal?: AbortSignal,
  ): Promise<Blob> {
    const response = await fetch(
      `${this.#baseUrl}/threads/${encodeURIComponent(threadId)}/attachments/${encodeURIComponent(attachmentId)}`,
      { headers: this.#headers(), credentials: "include", signal },
    );
    if (!response.ok) {
      const details = await readResponseBody(response);
      throw new ApiError(errorMessage(details, response.status), response.status, details);
    }
    return await response.blob();
  }

  deleteAttachment(threadId: string, attachmentId: string): Promise<void> {
    return this.#request<void>(
      `/threads/${encodeURIComponent(threadId)}/attachments/${encodeURIComponent(attachmentId)}`,
      { method: "DELETE" },
    );
  }

  getThreadContext(threadId: string, signal?: AbortSignal): Promise<RawThreadContext> {
    return this.#request<RawThreadContext>(
      `/threads/${encodeURIComponent(threadId)}/context/raw`,
      { signal },
    );
  }

  compactThread(threadId: string): Promise<CompactThreadResponse> {
    return this.#request<CompactThreadResponse>(
      `/threads/${encodeURIComponent(threadId)}/compact`,
      { method: "POST" },
    );
  }

  listPendingPrivateValueConsents(
    threadId: string,
    signal?: AbortSignal,
  ): Promise<PrivateValueConsentRequest[]> {
    return this.#request<PrivateValueConsentRequest[]>(
      `/threads/${encodeURIComponent(threadId)}/private-value-consents/pending`,
      { signal },
    );
  }

  decidePrivateValueConsent(
    threadId: string,
    consentId: string,
    approve: boolean,
  ): Promise<unknown> {
    return this.#request(
      `/threads/${encodeURIComponent(threadId)}/private-value-consents/${encodeURIComponent(consentId)}`,
      { method: "POST", body: JSON.stringify({ approve, one_shot: true }) },
    );
  }

  resumePrivateValueConsent(
    threadId: string,
    consentId: string,
  ): Promise<{ reply: string }> {
    return this.#request<{ reply: string }>(
      `/threads/${encodeURIComponent(threadId)}/private-value-consents/${encodeURIComponent(consentId)}/resume`,
      { method: "POST" },
    );
  }

  listPrivateValueActions(threadId: string): Promise<PrivateValueAction[]> {
    return this.#request<PrivateValueAction[]>(
      `/threads/${encodeURIComponent(threadId)}/private-value-actions`,
    );
  }

  discardPrivateValueAction(threadId: string, consentId: string): Promise<unknown> {
    return this.#request(
      `/threads/${encodeURIComponent(threadId)}/private-value-actions/${encodeURIComponent(consentId)}`,
      { method: "DELETE" },
    );
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

function queryString(input: object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(input)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
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
