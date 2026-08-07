# User execution extensibility

Status: Partially implemented

Implemented foundation: versioned SQLite storage scoped by tenant and user, typed validation for
personal skills, MCP servers, capability profiles, agents, defaults, and qualified references,
optimistic concurrency, principal-scoped coarse read/update/validate/delete APIs under
`/me/execution-config`, and a console JSON editor with validation, save, and reset controls.
Principal-aware execution options, thread creation, live personal-skill
prompt resolution, personal agent composition/defaults, and personal-resource ownership checks are
also implemented. Shared resources expose qualified `shared:` IDs while legacy unqualified shared
names remain accepted. The console also manages write-only static MCP credential headers with
create, rotation, and confirmed deletion flows.

Still pending: interactive OAuth connection and refresh flows, immutable thread resource version metadata
and pinning, lifecycle cleanup, sharing, and guided console editors for skills and agents. Granular principal-scoped resource CRUD APIs for skills, MCP servers,
capability profiles, and agents are now available alongside the coarse config API; the console now
supports guided skill and agent creation/removal, while MCP and capability-profile editors remain
pending. Personal capability profiles can use tenant-approved tools and user-owned MCP servers.
User-owned servers are restricted to public HTTPS destinations, revalidated on every request, and
gated by the tenant custom-MCP policy. Static authorization or API-key headers can be stored in the
write-only encrypted personal credential store and are resolved live for each thread run.
Personal execution configs also receive a reserved `user:minigent-user-tools` capability profile
referencing `shared:minigent-user-mcp`. This is an in-process, principal-scoped Minigent tool
provider, not a user-owned HTTP MCP server; it exposes the authenticated user's status, config,
access, and credential-management operations without storing a loopback URL or credentials.

profiles, and third-party MCP tools. It intentionally documents the product and runtime model
before implementation.

## Context

Minigent currently resolves execution configuration by tenant. A tenant execution config owns
its LLM settings, tools and MCP servers, skills, capability profiles, and agent presets. A thread
can select configured skill names, a capability profile, an LLM profile, or an agent preset, but
those resources must already exist in the tenant config.

That model supports shared administration, but it makes ordinary user customization depend on a
tenant execution-config edit. It prevents users from keeping personal instruction tweaks,
assembling personal agents, and connecting their own third-party tools without changing shared
configuration.

User execution configuration should therefore be a first-class additive layer. Tenant config
continues to provide shared defaults and reusable resources; it is not, by default, an exhaustive
catalog of everything a user may create.

## Goals

- Let users create and edit personal skills, including user-level system-prompt instructions.
- Let users connect personal third-party MCP servers and use them in capability profiles.
- Let users compose shared and personal resources into personal agent presets.
- Keep personal resources isolated by tenant and user unless the owner explicitly shares them.
- Resolve a coherent effective execution config from shared tenant resources and a user overlay.
- Keep thread behavior understandable when a selected resource is later edited or deleted.
- Keep credentials separate from ordinary execution-config JSON and thread state.
- Preserve current tenant-only configuration and API behavior during migration.
- Make deployment governance an optional policy layer rather than the core customization model.

## Non-goals for the first iteration

- A public marketplace for agents, skills, or MCP servers.
- Executing arbitrary uploaded native code inside the Minigent process.
- Automatically trusting third-party tools with private values or privileged identity forwarding.
- Cross-tenant sharing.
- Collaborative editing or branching of the same resource.
- A complete team/group hierarchy beyond tenant and user ownership.

## Design principles

### User configuration is additive

The primary model is:

```text
tenant execution config = shared defaults and shared resource catalog
user execution config   = additive personal resource overlay
thread execution config = selected composition of shared and personal resources
```

A user overlay is not limited to narrowing a tenant resource list. A user can introduce a new
skill, MCP server, capability profile, or agent preset that does not exist in tenant config.

Deployments may still enforce explicit operational policies, such as disabling outbound custom
MCP connections or restricting private-network destinations. Such policies are separate from the
resource composition model and should not make tenant approval the default workflow.

### Definitions and selections are different

A resource definition describes an agent, skill, capability profile, or MCP server. A selection
chooses definitions for a thread. Personal defaults are preferences, not authorization grants.

### Credentials are references

Saved user execution config may contain a credential reference but must not contain reusable
plaintext credentials. Static credential headers are stored in encrypted, tenant-and-user-scoped
rows under `MINIGENT_ADMIN_DB_PATH`; `MINIGENT_ADMIN_ENCRYPTION_KEY` is mandatory for credential
APIs and runtime resolution. The write-only API is:

```text
GET    /me/execution-credentials
PUT    /me/execution-credentials/{credential_ref}
DELETE /me/execution-credentials/{credential_ref}
```

PUT accepts `header_name`, `header_value`, and optional `expected_version`. Responses and list
results expose metadata and versions but never return `header_value`. Runtime resolution reloads
the current value for every run, so rotation does not require rewriting the execution config or
thread. Interactive OAuth tokens and refresh coordination remain a future connection-flow layer.

### Shared and personal names are unambiguous

Shared and personal resources may have the same display name. Stable qualified references avoid
merge ambiguity:

```text
shared:coding-workspace
shared:inspect
user:python-style
user:product-engineer
```

The API should return qualified IDs. Clients may omit the namespace in display text when the
selection is unambiguous.

## Resource model

### Personal skills

A personal skill can provide instructions and the same non-secret metadata supported by a shared
skill:

```json
{
  "id": "user:python-style",
  "name": "python-style",
  "description": "My preferred Python conventions",
  "system_prompt": "Prefer small typed functions, pytest, and concise trade-off explanations.",
  "workspace_scope": "backend"
}
```

Users can:

- create a skill from scratch;
- copy a shared skill into a personal resource and modify it;
- select multiple compatible shared and personal skills for a thread;
- choose a personal default skill or skill set;
- later publish or promote a copy through a separate sharing workflow.

A personal skill is model instruction, not native code. Any requested tools are resolved through
the selected capability and effective tool registry rather than becoming available merely because
the skill names them.

### Personal MCP servers

A user can register a remote MCP endpoint as a personal resource:

```json
{
  "id": "user:linear",
  "name": "linear",
  "url": "https://mcp.example.com/linear",
  "credential_ref": "oauth:linear-primary",
  "allowed_tools": ["list_issues", "create_issue"],
  "timeout_seconds": 30,
  "result_redaction": {"mode": "best_effort"}
}
```

The personal MCP representation should support the relevant existing MCP server behavior,
including protocol version, allowed tools, timeout, result redaction, path policy, private-value
policy, and per-tool approval policy. Static `Authorization` headers and equivalent reusable
secrets should be represented by `credential_ref` rather than returned from or accepted through
ordinary resource APIs.

Identity forwarding is not implicitly inherited from a shared server or tenant. A personal
server uses its user's credential binding. Privileged deployment identity forwarding remains a
separate explicitly configured facility.

### Personal capability profiles

A personal capability profile composes shared or personal MCP servers and any supported local
capability choices:

```json
{
  "id": "user:product-tools",
  "name": "product-tools",
  "description": "My issue tracker and source hosting tools",
  "mcp_server_refs": ["user:linear", "user:github"]
}
```

Unlike the current tenant representation, which references MCP servers defined elsewhere in the
same tenant config by name, user resources should use qualified stable references. This avoids
copying server definitions into every profile and makes credential ownership explicit.

Capability profiles can combine personal and shared servers. They do not copy credentials into
the profile.

### Personal agent presets

A personal agent preset is a reusable composition:

```json
{
  "id": "user:product-engineer",
  "name": "product-engineer",
  "description": "My coding and product workflow",
  "skill_refs": ["shared:coding-workspace", "user:python-style"],
  "capability_profile_ref": "user:product-tools",
  "llm_profile": "claude"
}
```

A personal agent may reference a tenant-approved named LLM profile with `llm_profile`. It cannot
embed provider credentials or define an arbitrary provider endpoint. Explicit thread-level LLM
selection still takes precedence over the agent's profile.


## Proposed storage

Use dedicated versioned records instead of generic tenant-user metadata. A single JSON overlay is
the smallest compatibility-oriented first step:

```text
user_execution_configs
  tenant_id
  user_id
  config_json
  version
  created_at
  updated_at
```

Conceptual payload:

```json
{
  "defaults": {
    "agent_ref": "user:product-engineer"
  },
  "skills": {
    "items": []
  },
  "mcp_servers": {
    "items": []
  },
  "capability_profiles": {
    "items": []
  },
  "agents": {
    "items": []
  }
}
```

The store is scoped by both tenant and authenticated user. Read and mutation methods must always
require both identifiers. Versioned optimistic concurrency should prevent two clients from
silently overwriting each other's edits.

If independent resource sharing, querying, or audit history becomes important, these resources
can later move to normalized tables without changing their qualified IDs or API representation.

Credential bindings are stored separately:

```text
user_tool_credentials
  tenant_id
  user_id
  credential_id
  provider_or_type
  encrypted_payload
  key_version
  status
  created_at
  updated_at
```

The exact encrypted credential schema can reuse or generalize Minigent's existing encrypted OAuth
storage. Execution-config APIs expose only opaque credential references and safe metadata.

## Effective execution resolution

Today, execution is resolved by tenant. User extensibility requires principal-aware resolution:

```python
tenant_context = tenant_execution_resolver.resolve(principal.tenant_id)
user_overlay = user_execution_store.get(principal.tenant_id, principal.user_id)
effective = merge_execution_config(tenant_context.config, user_overlay)
```

A dedicated API may expose this as:

```python
execution_resolver.resolve_for_principal(principal)
```

Resolution should produce:

- the tenant's shared resources;
- the authenticated user's personal resources;
- qualified lookup indexes;
- resolved user defaults with fallback to shared tenant defaults;
- a tool registry built only for the thread's selected shared and personal capability resources;
- resource source and version metadata for diagnostics.

Personal resources are additive. They do not mutate the cached tenant context. Cache keys for a
fully resolved context must include tenant ID, user ID, tenant config version, and user config
version. Tenant updates invalidate shared contexts; user updates invalidate only that user's
resolved contexts.

## Selection and merge behavior

Resolution follows these rules:

1. Preserve all valid shared tenant resources under `shared:` qualified IDs.
2. Preserve all valid personal resources under `user:` qualified IDs.
3. Do not overwrite a shared resource when a personal display name collides.
4. Resolve references after both catalogs are assembled.
5. Reject dangling references in user config with actionable validation errors.
6. Use a valid personal default when configured; otherwise fall back to the tenant default.
7. Return qualified IDs from execution-options and thread APIs.
8. Continue accepting legacy unqualified tenant names where they resolve unambiguously.

An explicit thread selection overrides an agent/default selection using the existing behavior.
The difference is that each selection may now reference either source.

## Thread lifecycle and versioning

The current implementation stores personal `user:` references and the execution owner's user ID
on new threads, while normalizing selected shared resources to their legacy unqualified names for
compatibility. Runtime resolution reloads the owner's current personal skill definition on every
run. Full qualified shared references, per-resource thread versions, and pinning remain the target
model below.

Threads should eventually store selected qualified references and the resource versions resolved
when the thread was created:

```json
{
  "agent_ref": "user:product-engineer",
  "agent_version": 4,
  "skill_refs": [
    {"id": "shared:coding-workspace", "version": 2},
    {"id": "user:python-style", "version": 7}
  ],
  "capability_profile_ref": {
    "id": "user:product-tools",
    "version": 3
  }
}
```

The default first-iteration behavior is **live resolution**: a thread keeps its references but
uses the current definitions when it runs. This matches a user's expectation that fixing a
personal prompt or reconnecting a tool affects existing conversations.

The stored versions support visibility and change detection. A later pinning feature can preserve
immutable resource revisions for reproducible threads. Credential values are never snapshotted
into thread storage.

If a referenced personal resource is deleted or becomes invalid, the thread remains readable but
a new run fails with a specific missing-resource error and offers the user a replacement. It must
not silently substitute a different same-named resource.

## API sketch

Self-service routes operate on the authenticated principal and do not accept arbitrary user IDs:

```text
GET    /me/execution-config
PUT    /me/execution-config
POST   /me/execution-config/validate
DELETE /me/execution-config

GET    /me/skills
POST   /me/skills
GET    /me/skills/{resource_id}
PUT    /me/skills/{resource_id}
DELETE /me/skills/{resource_id}

GET    /me/mcp-servers
POST   /me/mcp-servers
PUT    /me/mcp-servers/{resource_id}
DELETE /me/mcp-servers/{resource_id}
POST   /me/mcp-servers/{resource_id}/probe

GET    /me/capability-profiles
POST   /me/capability-profiles
PUT    /me/capability-profiles/{resource_id}
DELETE /me/capability-profiles/{resource_id}

GET    /me/agents
POST   /me/agents
PUT    /me/agents/{resource_id}
DELETE /me/agents/{resource_id}
```

The following resource-specific routes are implemented in addition to the coarse document API:

```text
GET    /me/{skills|mcp-servers|capability-profiles|agents}
GET    /me/{resource_type}/{resource_id}
PUT    /me/{resource_type}/{resource_id}
DELETE /me/{resource_type}/{resource_id}
```

Resource writes use the same `expected_version` optimistic-concurrency field and return the updated
resource plus its config version. Resource payloads are validated against the typed user execution
models before storage; IDs are principal-scoped and must use the `user:` namespace.


The implemented coarse API uses this update envelope:

```json
{
  "config": {
    "skills": {"items": []},
    "mcp_servers": {"items": []},
    "capability_profiles": {"items": []},
    "agents": {"items": []}
  },
  "expected_version": 0
}
```

`expected_version` is optional. Zero means that no record is expected; subsequent updates use the
version returned by the prior read or write. A mismatch returns HTTP 409. Validation returns a
normalized config and errors without writing. Reads return HTTP 404 until a config exists, and
deletes accept `expected_version` as an optional query parameter. Storage endpoints return a
service-unavailable response when the admin SQLite store is not configured; validation remains
available because it does not require storage.

Existing execution discovery should become principal-aware:

```text
GET /execution-options
```

It returns shared and personal options, their source, qualified ID, version, and description. It
must not expose another user's private resources or credentials.

Credential connection routes should use provider-specific OAuth or write-only secret flows and
return only opaque bindings:

```text
GET    /me/tool-credentials
POST   /me/tool-credentials/{provider}/login
DELETE /me/tool-credentials/{credential_id}
```

A generic write-only secret flow may be added for providers without OAuth, but the submitted value
is never returned by read APIs.

## Optional deployment policy

Some deployments need network and data controls. These are optional operational constraints on
personal resources, not a requirement that every personal definition appear in tenant config.
Possible policy settings include:

```text
allow_user_execution_config
allow_user_custom_mcp_servers
allowed_mcp_url_schemes
allowed_mcp_host_patterns
denied_mcp_network_ranges
max_user_skills
max_user_mcp_servers
max_user_capability_profiles
max_user_agents
```

Defaults for a self-hosted/local deployment should favor extensibility. Managed deployments can
choose stricter outbound-network policy. Network destination validation must be applied both when
a server is saved and when it is contacted so DNS changes cannot bypass the active policy.

Existing deployment MCP catalog assignment remains useful for managed shared servers, but it is
not the storage model for user-created servers.

## Security and isolation requirements

User extensibility requires isolation without turning every edit into an administrator approval:

- Scope every personal resource and credential lookup by tenant ID and user ID.
- Never include reusable credential values in config reads, logs, thread state, audit values, run
  events, or model context.
- Apply tool timeouts, result redaction, private-value policy, consent checks, and tool-call audit
  behavior to personal MCP tools as well as shared tools.
- Do not grant a personal server privileged deployment identity forwarding implicitly.
- Validate outbound destinations under the active deployment network policy at connection time.
- Attribute MCP calls to tenant, user, thread, server resource, and tool without recording secret
  arguments.
- Keep personal resources private by default.
- Disable personal resource execution when a membership is suspended or deleted, and remove or
  revoke credential bindings through user deprovisioning.
- Treat third-party tool output as untrusted model input under the same boundaries as existing MCP
  output.
- Preserve current explicit consent requirements when private placeholders would be disclosed.

A user-authored system prompt is expected behavior. It is not treated as tenant-admin content, but
it cannot itself bypass runtime tool or private-value boundaries.

## Sharing and promotion

The first iteration keeps personal resources private. A later explicit workflow may support:

```text
personal resource -> publish copy -> tenant-owned shared resource
```

Publishing creates a new shared definition with tenant ownership; it does not change the original
personal resource in place. Credentials are never promoted automatically. Shared server
publication requires a separately configured shared credential or identity model.

## Compatibility

- Deployments without a user execution store behave exactly as they do today.
- Threads with legacy unqualified skill, profile, and agent names resolve against shared tenant
  resources.
- Existing tenant execution config remains valid and remains the source of shared defaults.
- Existing user/role MCP catalog assignments continue to govern managed catalog servers.
- Existing clients can ignore source/version fields and continue selecting unqualified shared
  names during migration.

## Implementation phases

### Phase 1: Storage and validation

Status: implemented for the coarse user execution-config document and self-service API.

- Add versioned user execution-config storage.
- Define personal resource models and qualified references.
- Add principal-scoped read/update/validate APIs.
- Keep runtime behavior unchanged while validating round trips and isolation.

### Phase 2: Skills and agents

Status: implemented for principal-aware options, thread creation, native and peer prompt loading,
live personal-skill resolution, personal defaults, and personal-resource ownership enforcement.
Personal agents may compose shared and personal skills plus shared or personal capability profiles.

- Merge personal skills and agents into principal execution options.
- Resolve shared and personal skill references during thread creation and runs.
- Add personal defaults and web-console editors. Backend defaults are implemented; dedicated
  console editors remain pending.

### Phase 3: Personal MCP and capabilities

Status: partially implemented. User-owned MCP server and capability-profile models are stored and
validated. Personal capability profiles execute as narrowing-only overlays over tenant-approved
local tools and shared MCP servers in native and peer/broker runtimes. They may also connect to
user-owned MCP servers when tenant custom-server policy allows it. Personal servers must use public
HTTPS endpoints; local/private network destinations are rejected at selection and every outbound
request, environment proxies are disabled, sensitive static headers are forbidden, and tenant/user
resource ownership remains enforced. Static credential headers are encrypted at rest, exposed only
through write-only principal-scoped APIs, and resolved live without appearing in execution options,
thread records, or API responses. Interactive OAuth connection and refresh flows remain pending.

- Add user-owned MCP server and capability-profile models. Implemented.
- Execute personal capability profiles over approved local tools and shared MCP servers.
  Implemented.
- Add encrypted static credential references and write-only connection APIs. Implemented.
- Add interactive OAuth authorization and refresh flows.
- Build per-principal/per-thread MCP registries that include credential-free and static-credential
  personal servers. Implemented.
- Add server probing, diagnostics, redacted audit events, and deprovisioning cleanup.

### Phase 4: Version visibility and lifecycle UX

- Store resolved references and versions on threads.
- Detect changed or missing resources and return actionable errors.
- Add clone, export, import, and optional pinning workflows.

### Phase 5: Sharing

- Add explicit publication/promotion to tenant-owned resources.
- Add review and ownership transfer only where a deployment requires them.

## Open questions

- Should the first storage format be one versioned overlay document or normalized resource rows?
- Should personal capability profiles be able to reference shared MCP servers that are currently
  governed by subject catalog assignments, and how should that be presented in validation?
- Which local tools, if any, should users be able to include directly in personal capabilities?
- Should personal LLM profiles be part of the same overlay or remain a separate credential and
  model-preference feature?
- Which resource edits should increment a thread-visible version when only descriptions change?
- Should live resolution be configurable per thread before full immutable revision storage exists?
- What import/export format should be portable across Minigent installations without carrying
  credentials?
