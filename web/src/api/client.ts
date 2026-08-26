export type Authentication =
  | { mode: "session" }
  | { mode: "development"; tenantId: string; userId: string; isAdmin: boolean }
  | { mode: "bearer"; token: string };

export interface SessionPrincipal {
  user_id: string;
  tenant_id: string;
  is_admin: boolean;
}

export interface SessionStatusResponse {
  enabled: boolean;
  authenticated: boolean;
  principal?: SessionPrincipal | null;
}

export interface TenantContextResponse {
  principal: SessionPrincipal;
  tenant_id: string;
  slug?: string | null;
  status?: TenantStatus | null;
  plan?: string | null;
  region?: string | null;
  features: Record<string, boolean>;
  limits: Record<string, number | string | boolean | null>;
  execution_config_version?: number | null;
  entitlements_version?: number | null;
  membership_id?: string | null;
  membership_email?: string | null;
  membership_display_name?: string | null;
  user_role?: TenantUserRole | null;
  user_status?: TenantUserStatus | null;
  membership_metadata: Record<string, unknown>;
}

export interface PasswordSetupStatus {
  valid: boolean;
  username?: string | null;
  expires_at?: string | null;
}

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
  id?: string | null;
  display_name?: string | null;
  source?: "shared" | "user" | null;
  version?: number | null;
}

export interface ExecutionOptionSection {
  default?: string | null;
  defaults?: string[] | null;
  items: ExecutionOptionItem[];
}

export interface ExecutionLlmOptionItem extends ExecutionOptionItem {
  input_modalities?: string[] | null;
  image_input_allowed: boolean;
  document_input_allowed: boolean;
  document_input_reason?: "disabled" | "backend_unsupported" | "profile_unsupported" | null;
  image_input_reason?: "disabled" | "backend_unsupported" | "profile_unsupported" | null;
  capability_declared: boolean;
}

export interface ExecutionLlmOptionSection {
  default?: string | null;
  effective_default: ExecutionLlmOptionItem;
  items: ExecutionLlmOptionItem[];
}

export interface ExecutionAgentOptionItem extends ExecutionOptionItem {
  skill_name?: string | null;
  skills?: string[] | null;
  capability_profile?: string | null;
  llm_profile?: string | null;
}

export interface ExecutionAgentOptionSection {
  default?: string | null;
  items: ExecutionAgentOptionItem[];
}

export interface ExecutionOptionsResponse {
  tenant_id: string;
  skills: ExecutionOptionSection;
  capability_profiles: ExecutionOptionSection;
  llm_profiles: ExecutionLlmOptionSection;
  agents: ExecutionAgentOptionSection;
}

export interface UserExecutionConfig {
  tenant_id: string;
  user_id: string;
  config: Record<string, unknown>;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface UserExecutionConfigValidation {
  valid: boolean;
  errors: string[];
  normalized_config?: Record<string, unknown> | null;
}

export interface UserResourceListResponse {
  items: Record<string, unknown>[];
  version?: number | null;
}

export interface UserResourceResponse {
  resource: Record<string, unknown>;
  version: number;
}

export interface UserExecutionCredential {
  tenant_id: string;
  user_id: string;
  credential_ref: string;
  header_name: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface UserExecutionCredentialListResponse {
  items: UserExecutionCredential[];
}

export interface UserMCPServerAccess {
  id: string;
  name: string;
  source: "user" | "shared";
  allowed_tools?: string[] | null;
  credential_configured: boolean;
}

export interface UserMCPAccess {
  tenant_id: string;
  user_id: string;
  endpoint_path: string;
  personal_mcp_servers_allowed: boolean;
  personal_servers: UserMCPServerAccess[];
  shared_servers: UserMCPServerAccess[];
}

export interface UserMCPStatusFinding {
  code: string;
  severity: "info" | "warning" | "error";
  message: string;
  remediation: string;
}

export interface UserMCPStatus {
  tenant_id: string;
  user_id: string;
  endpoint_path: string;
  execution_configured: boolean;
  execution_config_version?: number | null;
  encrypted_credentials_available: boolean;
  personal_mcp_servers_allowed: boolean;
  skills: number;
  mcp_servers: number;
  capability_profiles: number;
  agents: number;
  findings: UserMCPStatusFinding[];
}

export interface UserExecutionCredentialInput {
  header_name: string;
  header_value: string;
  expected_version?: number | null;
}

export type ThreadStatus = "idle" | "running" | "error";

export interface ThreadListItem {
  thread_id: string;
  title: string;
  title_source?: "generated" | "semantic" | "manual" | null;
  title_updated_at?: string | null;
  pinned_at?: string | null;
  archived_at?: string | null;
  status: ThreadStatus;
  skill_name?: string | null;
  capability_profile?: string | null;
  llm_profile?: string | null;
  parent_thread_id?: string | null;
  fork_message_id?: string | null;
  compacted_through_message_id?: string | null;
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

export interface ThreadSearchMatch {
  message_id: string;
  role: "user" | "assistant";
  snippet: string;
  created_at: string;
}

export interface ThreadSearchResult {
  thread: ThreadListItem;
  match_count: number;
  matches: ThreadSearchMatch[];
}

export interface ThreadSearchResponse {
  results: ThreadSearchResult[];
  total: number;
  limit: number;
  offset: number;
}

export interface AttachmentPartBase {
  mime_type: string;
  attachment_id: string;
}

export interface ImagePart extends AttachmentPartBase {
  type: "image";
  detail: "auto" | "low" | "high";
}

export interface DocumentPart extends AttachmentPartBase {
  type: "document";
  filename: string;
}

export interface TextPart {
  type: "text";
  text: string;
}

export type MessagePart = TextPart | ImagePart | DocumentPart;

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

export interface DocumentInputConfig {
  enabled: boolean;
  max_bytes: number;
  max_documents: number;
  max_total_bytes: number;
  allowed_mime_types: string[];
}

export interface PublicConfig {
  document_input: DocumentInputConfig;
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

export interface ThreadLineageResponse {
  thread: ThreadListItem;
  parent: ThreadListItem | null;
  children: ThreadListItem[];
  siblings: ThreadListItem[];
}

export interface ForkThreadResponse {
  thread_id: string;
  parent_thread_id: string;
  fork_message_id: string;
}

export interface CompactThreadResponse {
  thread_id: string;
  source_thread_id: string;
  fork_message_id: string | null;
  compacted_through_message_id: string | null;
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
  provisioning_profile?: "none" | "generic-v1";
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

export interface AdminExecutionConfig {
  version: number;
  config: Record<string, unknown>;
}

export interface AdminTenantExecutionConfig extends AdminExecutionConfig {
  tenant_id: string;
}

export interface AdminMcpServerCatalogItem {
  id: string;
  title: string;
  description: string;
  detail?: string | null;
  server: Record<string, unknown>;
}

export interface AdminMcpServerCatalog {
  items: AdminMcpServerCatalogItem[];
  managed: boolean;
  allow_custom_mcp_servers: boolean;
}

export interface AdminExternalGrantProvider {
  id: string;
  title: string;
  description: string;
  allowed_permissions: string[];
  resource_discovery_available: boolean;
  audit_available: boolean;
}

export interface AdminExternalGrantProviderList {
  providers: AdminExternalGrantProvider[];
}

export interface AdminExternalGrant {
  resource_id: string;
  subject_id: string;
  permission: string;
  enabled: boolean;
  updated_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AdminExternalGrantList {
  tenant_id: string;
  provider_id: string;
  grants: AdminExternalGrant[];
}

export interface AdminExternalGrantResource {
  resource_id: string;
  kind: string;
  label: string;
  allowed_permissions: string[];
  configured: boolean;
  enabled: boolean;
}

export interface AdminExternalGrantResourceList {
  tenant_id: string;
  provider_id: string;
  resources: AdminExternalGrantResource[];
}

export interface AdminExternalGrantAuditState {
  permission: string;
  enabled: boolean;
}

export interface AdminExternalGrantAudit {
  audit_id: number;
  resource_id: string;
  subject_id: string;
  actor_id: string;
  operation: string;
  previous: AdminExternalGrantAuditState | null;
  resulting: AdminExternalGrantAuditState | null;
  created_at: string;
}

export interface AdminExternalGrantAuditList {
  tenant_id: string;
  provider_id: string;
  entries: AdminExternalGrantAudit[];
  next_cursor: number | null;
}

export interface AdminExternalGrantInput {
  resource_id: string;
  subject_id: string;
  permission: string;
  enabled: boolean;
}

export type AdminUserDeprovisioningState = "pending" | "processing" | "completed" | "dead_letter";

export interface AdminUserDeprovisioningEvent {
  id: string;
  tenant_id: string;
  user_record_id: string;
  user_id: string;
  target_status: TenantUserStatus;
  actor_user_id: string;
  state: AdminUserDeprovisioningState;
  attempts: number;
  next_attempt_at: string;
  claimed_at: string | null;
  completed_at: string | null;
  last_error: string | null;
  assignment_removed: boolean;
  grants_disabled: number;
  created_at: string;
  updated_at: string;
}

export interface AdminUserDeprovisioningEventList {
  tenant_id: string;
  events: AdminUserDeprovisioningEvent[];
  limit: number;
  offset: number;
  total: number;
  next_offset: number | null;
}

export interface AdminMcpServerCatalogPolicy {
  tenant_id: string;
  item_ids: string[];
  allow_custom_mcp_servers: boolean;
  require_subject_assignment: boolean;
  version: number;
  updated_by?: string | null;
  updated_at: string;
}

export interface AdminMcpServerCatalogPolicyInput {
  item_ids: string[];
  allow_custom_mcp_servers: boolean;
  require_subject_assignment: boolean;
}

export type AdminMcpCatalogSubjectType = "user" | "role";

export interface AdminMcpServerCatalogAssignment {
  tenant_id: string;
  subject_type: AdminMcpCatalogSubjectType;
  subject_id: string;
  item_ids: string[];
  version: number;
  updated_by?: string | null;
  updated_at: string;
}

export interface AdminMcpServerCatalogAssignmentList {
  tenant_id: string;
  assignments: AdminMcpServerCatalogAssignment[];
}

export interface AdminMcpServerCatalogAccessPreviewEntry {
  user_id: string;
  display_name?: string | null;
  email?: string | null;
  role: TenantUserRole;
  status: TenantUserStatus;
  source: string;
  item_ids: string[];
  denied: boolean;
}

export interface AdminMcpServerCatalogAccessPreview {
  tenant_id: string;
  require_subject_assignment: boolean;
  users: AdminMcpServerCatalogAccessPreviewEntry[];
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

export interface TenantOAuthCredentialStatus {
  tenant_id: string;
  provider_id: string;
  source: "pi";
  connected: boolean;
  account_id?: string | null;
  expires_at?: string | null;
}

export interface AdminCredentialStatus {
  configured: boolean;
  username?: string | null;
  disabled: boolean;
  managed_externally: boolean;
  updated_at?: string | null;
}

export interface AdminCredentialSetup {
  username: string;
  setup_token: string;
  expires_at: string;
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
  detail?: unknown;
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

  getSession(signal?: AbortSignal): Promise<SessionStatusResponse> {
    return this.#request<SessionStatusResponse>("/auth/session", { signal });
  }

  login(username: string, password: string): Promise<SessionStatusResponse> {
    return this.#request<SessionStatusResponse>("/auth/session", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  }

  logout(): Promise<void> {
    return this.#request<void>("/auth/session", { method: "DELETE" });
  }

  getPasswordSetupStatus(token: string): Promise<PasswordSetupStatus> {
    return this.#request<PasswordSetupStatus>("/auth/password/setup/status", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
  }

  completePasswordSetup(token: string, password: string): Promise<SessionStatusResponse> {
    return this.#request<SessionStatusResponse>("/auth/password/setup", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    });
  }

  getExecutionOptions(signal?: AbortSignal): Promise<ExecutionOptionsResponse> {
    return this.#request<ExecutionOptionsResponse>("/execution-options", { signal });
  }

  getUserExecutionConfig(signal?: AbortSignal): Promise<UserExecutionConfig> {
    return this.#request<UserExecutionConfig>("/me/execution-config", { signal });
  }

  validateUserExecutionConfig(config: Record<string, unknown>): Promise<UserExecutionConfigValidation> {
    return this.#request<UserExecutionConfigValidation>("/me/execution-config/validate", {
      method: "POST",
      body: JSON.stringify({ config }),
    });
  }

  updateUserExecutionConfig(
    config: Record<string, unknown>,
    expectedVersion?: number,
  ): Promise<UserExecutionConfig> {
    return this.#request<UserExecutionConfig>("/me/execution-config", {
      method: "PUT",
      body: JSON.stringify({ config, expected_version: expectedVersion }),
    });
  }

  deleteUserExecutionConfig(expectedVersion?: number): Promise<void> {
    const query = expectedVersion === undefined
      ? ""
      : `?expected_version=${encodeURIComponent(String(expectedVersion))}`;
    return this.#request<void>(`/me/execution-config${query}`, { method: "DELETE" });
  }

  listUserResources(
    resourceType: "skills" | "mcp-servers" | "capability-profiles" | "agents",
    signal?: AbortSignal,
  ): Promise<UserResourceListResponse> {
    return this.#request<UserResourceListResponse>(`/me/${resourceType}`, { signal });
  }

  getUserResource(
    resourceType: "skills" | "mcp-servers" | "capability-profiles" | "agents",
    resourceId: string,
    signal?: AbortSignal,
  ): Promise<UserResourceResponse> {
    return this.#request<UserResourceResponse>(
      `/me/${resourceType}/${encodeURIComponent(resourceId)}`,
      { signal },
    );
  }

  updateUserResource(
    resourceType: "skills" | "mcp-servers" | "capability-profiles" | "agents",
    resourceId: string,
    resource: Record<string, unknown>,
    expectedVersion?: number,
  ): Promise<UserResourceResponse> {
    return this.#request<UserResourceResponse>(
      `/me/${resourceType}/${encodeURIComponent(resourceId)}`,
      {
        method: "PUT",
        body: JSON.stringify({ resource, expected_version: expectedVersion }),
      },
    );
  }

  deleteUserResource(
    resourceType: "skills" | "mcp-servers" | "capability-profiles" | "agents",
    resourceId: string,
    expectedVersion?: number,
  ): Promise<void> {
    const query = expectedVersion === undefined
      ? ""
      : `?expected_version=${encodeURIComponent(String(expectedVersion))}`;
    return this.#request<void>(
      `/me/${resourceType}/${encodeURIComponent(resourceId)}${query}`,
      { method: "DELETE" },
    );
  }

  getUserMCPAccess(signal?: AbortSignal): Promise<UserMCPAccess> {
    return this.#request<UserMCPAccess>("/me/mcp-access", { signal });
  }

  getUserMCPStatus(signal?: AbortSignal): Promise<UserMCPStatus> {
    return this.#request<UserMCPStatus>("/me/mcp-status", { signal });
  }

  listUserExecutionCredentials(signal?: AbortSignal): Promise<UserExecutionCredentialListResponse> {
    return this.#request<UserExecutionCredentialListResponse>("/me/execution-credentials", {
      signal,
    });
  }


  updateUserExecutionCredential(
    credentialRef: string,
    input: UserExecutionCredentialInput,
  ): Promise<UserExecutionCredential> {
    return this.#request<UserExecutionCredential>(
      `/me/execution-credentials/${encodeURIComponent(credentialRef)}`,
      { method: "PUT", body: JSON.stringify(input) },
    );
  }

  deleteUserExecutionCredential(
    credentialRef: string,
    expectedVersion?: number,
  ): Promise<void> {
    const query = expectedVersion === undefined
      ? ""
      : `?expected_version=${encodeURIComponent(String(expectedVersion))}`;
    return this.#request<void>(
      `/me/execution-credentials/${encodeURIComponent(credentialRef)}${query}`,
      { method: "DELETE" },
    );
  }

  getTenantContext(signal?: AbortSignal): Promise<TenantContextResponse> {
    return this.#request<TenantContextResponse>("/tenant-context", { signal });
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

  getAdminTenant(tenantId: string, signal?: AbortSignal): Promise<AdminTenant> {
    return this.#request<AdminTenant>(`/admin/tenants/${encodeURIComponent(tenantId)}`, { signal });
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

  getAdminTenantUserCredential(
    tenantId: string,
    userRecordId: string,
    signal?: AbortSignal,
  ): Promise<AdminCredentialStatus> {
    return this.#request<AdminCredentialStatus>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/users/${encodeURIComponent(userRecordId)}/credential`,
      { signal },
    );
  }

  createAdminTenantUserCredentialSetup(
    tenantId: string,
    userRecordId: string,
    username: string,
  ): Promise<AdminCredentialSetup> {
    return this.#request<AdminCredentialSetup>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/users/${encodeURIComponent(userRecordId)}/credential/setup`,
      { method: "POST", body: JSON.stringify({ username }) },
    );
  }

  disableAdminTenantUserCredential(
    tenantId: string,
    userRecordId: string,
  ): Promise<{ disabled: boolean }> {
    return this.#request<{ disabled: boolean }>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/users/${encodeURIComponent(userRecordId)}/credential`,
      { method: "DELETE" },
    );
  }

  getTenantOpenAIOAuthCredential(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<TenantOAuthCredentialStatus> {
    return this.#request<TenantOAuthCredentialStatus>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/oauth/openai-codex`,
      { signal },
    );
  }

  importTenantOpenAIOAuthFromPi(
    tenantId: string,
    credential: Record<string, unknown>,
  ): Promise<TenantOAuthCredentialStatus> {
    return this.#request<TenantOAuthCredentialStatus>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/oauth/openai-codex/import/pi`,
      {
        method: "POST",
        body: JSON.stringify({ credential, acknowledge_transfer: true }),
      },
    );
  }

  deleteTenantOpenAIOAuthCredential(tenantId: string): Promise<void> {
    return this.#request<void>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/oauth/openai-codex`,
      { method: "DELETE" },
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

  listAdminExternalGrantProviders(signal?: AbortSignal): Promise<AdminExternalGrantProviderList> {
    return this.#request<AdminExternalGrantProviderList>("/admin/external-grant-providers", {
      signal,
    });
  }

  listAdminUserDeprovisioningEvents(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminUserDeprovisioningEventList> {
    return this.#request<AdminUserDeprovisioningEventList>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/user-deprovisioning-events`,
      { signal },
    );
  }

  retryAdminUserDeprovisioningEvent(
    tenantId: string,
    eventId: string,
  ): Promise<AdminUserDeprovisioningEvent> {
    return this.#request<AdminUserDeprovisioningEvent>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/user-deprovisioning-events/${encodeURIComponent(eventId)}/retry`,
      { method: "POST" },
    );
  }

  listAdminExternalGrants(
    tenantId: string,
    providerId: string,
    signal?: AbortSignal,
  ): Promise<AdminExternalGrantList> {
    return this.#request<AdminExternalGrantList>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/external-grants/${encodeURIComponent(providerId)}`,
      { signal },
    );
  }

  listAdminExternalGrantResources(
    tenantId: string,
    providerId: string,
    signal?: AbortSignal,
  ): Promise<AdminExternalGrantResourceList> {
    return this.#request<AdminExternalGrantResourceList>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/external-grants/${encodeURIComponent(providerId)}/resources`,
      { signal },
    );
  }

  listAdminExternalGrantAudit(
    tenantId: string,
    providerId: string,
    options: { limit?: number; before_id?: number } = {},
    signal?: AbortSignal,
  ): Promise<AdminExternalGrantAuditList> {
    return this.#request<AdminExternalGrantAuditList>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/external-grants/${encodeURIComponent(providerId)}/audit${queryString(options)}`,
      { signal },
    );
  }

  updateAdminExternalGrant(
    tenantId: string,
    providerId: string,
    input: AdminExternalGrantInput,
  ): Promise<AdminExternalGrant> {
    return this.#request<AdminExternalGrant>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/external-grants/${encodeURIComponent(providerId)}`,
      { method: "PUT", body: JSON.stringify(input) },
    );
  }

  deleteAdminExternalGrant(
    tenantId: string,
    providerId: string,
    resourceId: string,
    subjectId: string,
  ): Promise<void> {
    return this.#request<void>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/external-grants/${encodeURIComponent(providerId)}/${encodeURIComponent(resourceId)}?subject_id=${encodeURIComponent(subjectId)}`,
      { method: "DELETE" },
    );
  }

  getAdminDeploymentMcpServerCatalog(signal?: AbortSignal): Promise<AdminMcpServerCatalog> {
    return this.#request<AdminMcpServerCatalog>("/admin/mcp-server-catalog", { signal });
  }

  previewAdminMcpServerCatalogAccess(
    tenantId: string,
    requireSubjectAssignment: boolean,
    signal?: AbortSignal,
  ): Promise<AdminMcpServerCatalogAccessPreview> {
    return this.#request<AdminMcpServerCatalogAccessPreview>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/mcp-server-catalog-access-preview?require_subject_assignment=${requireSubjectAssignment}`,
      { signal },
    );
  }

  getAdminMcpServerCatalogPolicy(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminMcpServerCatalogPolicy> {
    return this.#request<AdminMcpServerCatalogPolicy>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/mcp-server-catalog-policy`,
      { signal },
    );
  }

  updateAdminMcpServerCatalogPolicy(
    tenantId: string,
    input: AdminMcpServerCatalogPolicyInput,
  ): Promise<AdminMcpServerCatalogPolicy> {
    return this.#request<AdminMcpServerCatalogPolicy>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/mcp-server-catalog-policy`,
      { method: "PUT", body: JSON.stringify(input) },
    );
  }

  deleteAdminMcpServerCatalogPolicy(tenantId: string): Promise<void> {
    return this.#request<void>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/mcp-server-catalog-policy`,
      { method: "DELETE" },
    );
  }

  listAdminMcpServerCatalogAssignments(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminMcpServerCatalogAssignmentList> {
    return this.#request<AdminMcpServerCatalogAssignmentList>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/mcp-server-catalog-assignments`,
      { signal },
    );
  }

  updateAdminMcpServerCatalogAssignment(
    tenantId: string,
    subjectType: AdminMcpCatalogSubjectType,
    subjectId: string,
    itemIds: string[],
  ): Promise<AdminMcpServerCatalogAssignment> {
    return this.#request<AdminMcpServerCatalogAssignment>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/mcp-server-catalog-assignments/${encodeURIComponent(subjectType)}/${encodeURIComponent(subjectId)}`,
      { method: "PUT", body: JSON.stringify({ item_ids: itemIds }) },
    );
  }

  deleteAdminMcpServerCatalogAssignment(
    tenantId: string,
    subjectType: AdminMcpCatalogSubjectType,
    subjectId: string,
  ): Promise<void> {
    return this.#request<void>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/mcp-server-catalog-assignments/${encodeURIComponent(subjectType)}/${encodeURIComponent(subjectId)}`,
      { method: "DELETE" },
    );
  }

  getAdminMcpServerCatalog(
    tenantId: string,
    signal?: AbortSignal,
  ): Promise<AdminMcpServerCatalog> {
    return this.#request<AdminMcpServerCatalog>(
      `/admin/tenants/${encodeURIComponent(tenantId)}/mcp-server-catalog`,
      { signal },
    );
  }

  getAdminExecutionConfig(signal?: AbortSignal): Promise<AdminExecutionConfig> {
    return this.#request<AdminExecutionConfig>("/admin/execution-config", { signal });
  }

  validateAdminExecutionConfig(
    config: Record<string, unknown>,
  ): Promise<AdminExecutionValidation> {
    return this.#request<AdminExecutionValidation>("/admin/execution-config/validate", {
      method: "POST",
      body: JSON.stringify({ config }),
    });
  }

  updateAdminExecutionConfig(
    config: Record<string, unknown>,
  ): Promise<AdminExecutionConfig> {
    return this.#request<AdminExecutionConfig>("/admin/execution-config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    });
  }

  deleteAdminExecutionConfig(): Promise<void> {
    return this.#request<void>("/admin/execution-config", { method: "DELETE" });
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

  listThreads(
    limit = 50,
    signal?: AbortSignal,
    options: { q?: string; archived?: boolean; pinned?: boolean } = {},
  ): Promise<ThreadListResponse> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (options.q) params.set("q", options.q);
    if (options.archived) params.set("archived", "true");
    if (options.pinned !== undefined) params.set("pinned", String(options.pinned));
    return this.#request<ThreadListResponse>(`/threads?${params.toString()}`, { signal });
  }

  searchThreads(
    query: string,
    signal?: AbortSignal,
    options: { scope?: "title" | "messages" | "all"; archived?: boolean; limit?: number } = {},
  ): Promise<ThreadSearchResponse> {
    const params = new URLSearchParams({
      q: query,
      scope: options.scope ?? "all",
      limit: String(options.limit ?? 20),
    });
    if (options.archived) params.set("archived", "true");
    return this.#request<ThreadSearchResponse>(`/search/threads?${params.toString()}`, { signal });
  }

  createThread(
    options: { agentName?: string; llmProfile?: string } = {},
    signal?: AbortSignal,
  ): Promise<{ thread_id: string }> {
    const body = {
      ...(options.agentName ? { agent_name: options.agentName } : {}),
      ...(options.llmProfile ? { llm_profile: options.llmProfile } : {}),
    };
    return this.#request<{ thread_id: string }>("/threads", {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    });
  }

  renameThread(threadId: string, title: string, signal?: AbortSignal): Promise<{ thread_id: string; title: string; title_source: "manual"; title_updated_at: string }> {
    return this.#request(`/threads/${encodeURIComponent(threadId)}/title`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
      signal,
    });
  }

  updateThreadOrganization(
    threadId: string,
    organization: { pinned?: boolean; archived?: boolean },
    signal?: AbortSignal,
  ): Promise<ThreadListItem> {
    return this.#request<ThreadListItem>(`/threads/${encodeURIComponent(threadId)}/organization`, {
      method: "PATCH",
      body: JSON.stringify(organization),
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

  getThreadLineage(threadId: string, signal?: AbortSignal): Promise<ThreadLineageResponse> {
    return this.#request<ThreadLineageResponse>(
      `/threads/${encodeURIComponent(threadId)}/lineage`,
      { signal },
    );
  }

  forkThread(threadId: string, messageId: string): Promise<ForkThreadResponse> {
    return this.#request<ForkThreadResponse>(
      `/threads/${encodeURIComponent(threadId)}/fork`,
      {
        method: "POST",
        body: JSON.stringify({ at_message_id: messageId }),
      },
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
      headers.set("X-Mindweft-Tenant-Id", this.#authentication.tenantId);
      headers.set("X-Mindweft-User-Id", this.#authentication.userId);
      if (this.#authentication.isAdmin) headers.set("X-Mindweft-Admin", "true");
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
