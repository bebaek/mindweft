# Dynamic tenant management

Status: Partially implemented

Implemented: SQLite-backed tenant registry, tenant lifecycle admin APIs and CLI commands,
soft deletion via tenant status, tenant seeding from execution-config tenants, tenant
domain registration, manual verification, and lookup APIs, tenant entitlement
CRUD/validation APIs, request-time tenant context resolution, structured tenant audit
metadata for tenant/domain/entitlement mutations, optional active-tenant enforcement with
`MINIGENT_TENANT_REGISTRY_REQUIRED`, execution-config version exposure in tenant context,
execution-config admin CRUD/validation with in-process resolver invalidation, and runtime
entitlement enforcement for `peer_agents`/`mcp` feature flags plus `max_threads`,
`max_messages_per_thread`/`max_messages`, and `max_thread_runs` limits.

Still pending or partial: request routing by tenant domain, broader quotas and rate limits,
registry and entitlement caching/cross-process invalidation, granular admin roles beyond
`is_admin=true`, complete execution-config mutation audit coverage, stricter referential
integrity between registry/config/entitlement/domain records, and moving all
tenant-specific defaults out of environment/static configuration.

## Context

Minigent supports tenant-scoped authentication, thread isolation, per-tenant execution
configuration, and an optional admin SQLite store. Dynamic tenant management extends that
model with runtime-managed tenant identity, lifecycle state, domains, entitlements, admin
operations, and audit metadata.

The current implementation remains compatibility-oriented: tenant registry records,
entitlements, and execution configs are related by tenant ID but can still be managed
independently in some paths. This preserves existing execution-config workflows while the
registry becomes the durable control-plane source of truth.

## Goals

- Manage tenant lifecycle at runtime without deployment-driven config edits.
- Keep request-time tenant resolution and authorization explicit and auditable.
- Preserve existing tenant thread isolation and execution-config behavior during migration.
- Support per-tenant status, domains/slugs, plan/entitlements, quotas, and execution config.
- Provide admin APIs and CLI surfaces for support and operations workflows.
- Keep global defaults static while tenant-specific state moves to a durable store.

## Non-goals for the first iteration

- Full billing integration.
- Automated custom-domain certificate provisioning.
- Multi-region tenant migration.
- Per-tenant database provisioning.
- Self-service public signup flows.

Those can build on the same registry later. The first version focuses on the control-plane
foundation.

## Terminology

- **Tenant ID**: immutable internal identifier used for authorization, thread isolation,
  audit records, and record lookup. Admin tenant creation accepts an explicit ID for
  compatibility or generates a UUID when omitted.
- **Slug**: unique, URL-friendly human identifier such as `acme` or `big-river-labs`.
  Slugs can currently be changed by admin update operations, subject to uniqueness.
- **Tenant registry**: durable source of truth for tenant identity, lifecycle state,
  domains, and operational metadata.
- **Execution config**: tenant-specific LLM, tool, MCP, skill, capability-profile,
  backend, and quality configuration.
- **Entitlements**: explicit feature flags and limits that determine what a tenant is
  allowed to use. Plan-derived entitlement expansion is not implemented yet.

## Implemented data model

The SQLite admin store creates the following registry/control-plane tables:

```text
tenants
  id
  slug
  name
  status: active | provisioning | suspended | archived | deleted
  plan
  region
  metadata_json
  created_by
  updated_by
  created_at
  updated_at

tenant_domains
  id
  tenant_id
  domain
  verified
  created_at

tenant_entitlements
  tenant_id
  features_json
  limits_json
  version
  updated_at

tenant_execution_configs
  tenant_id
  config_json
  version
  created_at
  updated_at
```

Important attributes such as `status`, `slug`, `plan`, and `region` are columns rather
than only JSON fields because they are used for filtering, authorization, support, and
future routing workflows. Flexible tenant metadata remains JSON.

Current caveat: the SQLite schema stores related records by `tenant_id`, but not every admin
path requires an existing registry tenant and the table definitions do not currently enforce
foreign-key constraints. This is intentional or at least tolerated during migration from the
older execution-config-only model, but it should be tightened when the registry becomes the
sole source of truth.

## Request-time behavior

The current request-time tenant flow is:

1. Authenticate the caller.
2. Read the tenant ID from trusted principal/auth material.
3. If the admin store is enabled, load the tenant registry record for the principal tenant.
4. If `MINIGENT_TENANT_REGISTRY_REQUIRED` is enabled, reject missing or non-active tenants.
5. Load tenant entitlements and execution-config version when a registry/admin store is
   available.
6. Attach `TenantContext` to request state for handlers and runtime policy checks.
7. Resolve execution config for the tenant.
8. Apply entitlement checks where currently wired.
9. Run thread/message/run operations with tenant-scoped storage.

`TenantContext` currently includes:

```text
tenant_id
slug
status
plan
region
features
limits
execution_config_version
entitlements_version
```

If the registry is not required, missing registry rows are allowed for compatibility and the
context falls back to the authenticated principal tenant ID. If the registry is required,
missing or inactive tenants are rejected before business logic runs.

## Runtime entitlement enforcement

Implemented runtime enforcement covers:

- `peer_agents`: required when the tenant execution config selects the peer-agent backend.
- `mcp`: required when the tenant execution config includes MCP servers.
- `max_threads`: enforced before thread creation.
- `max_messages_per_thread`: enforced before user message creation.
- `max_messages`: accepted as a fallback for `max_messages_per_thread`.
- `max_thread_runs`: enforced before non-streaming and streaming thread runs.

Broader quota coverage is still pending, including token usage, time-window rate limits,
tool-call counts, MCP-call counts, storage usage, attachment size, and concurrent-run
limits.

## Admin operations status

| Operation | Status | Notes |
| --- | --- | --- |
| List tenants | Implemented | Supports `status`, `plan`, `slug`, `limit`, and `offset`. |
| Create tenant | Implemented | Optional explicit ID; otherwise generated UUID. |
| Read tenant | Implemented | `GET /admin/tenants/{tenant_id}`. |
| Update tenant fields | Implemented | Supports slug, name, plan, region, metadata. |
| Activate tenant | Implemented | Status transition to `active`. |
| Suspend tenant | Implemented | Status transition to `suspended`. |
| Archive tenant | Implemented | Status transition to `archived`. |
| Delete tenant | Implemented | Soft delete by status transition to `deleted`. |
| Seed registry tenants | Implemented | Seeds from existing execution-config tenant IDs. |
| Add/list/delete domains | Implemented | Domains are unique by domain name. |
| Verify domains | Implemented | Manual admin verification. |
| Lookup domain | Implemented | Admin lookup from domain to tenant-domain record. |
| Read/update/delete entitlements | Implemented | Explicit features/limits JSON with versioning. |
| Validate entitlements | Implemented | Validation endpoint reports feature/limit errors. |
| Read/update/delete execution config | Implemented | Read responses redact secrets. |
| Validate execution config | Implemented | Parses and validates config shape. |
| Audit tenant mutations | Partial | Tenant/domain/entitlement mutations audited; execution-config mutation audit coverage should be completed. |
| Dry-run admin operations | Partial | Validation and seed dry-run exist; slug-change dry-run is not implemented. |

## API surface

Implemented tenant registry and control-plane routes include:

```text
GET    /admin/tenants
POST   /admin/tenants
POST   /admin/tenants/seed
GET    /admin/tenants/{tenant_id}
PATCH  /admin/tenants/{tenant_id}
POST   /admin/tenants/{tenant_id}/activate
POST   /admin/tenants/{tenant_id}/suspend
POST   /admin/tenants/{tenant_id}/archive
DELETE /admin/tenants/{tenant_id}

GET    /admin/tenant-domains/lookup
GET    /admin/tenants/{tenant_id}/domains
POST   /admin/tenants/{tenant_id}/domains
POST   /admin/tenants/{tenant_id}/domains/{domain_id}/verify
DELETE /admin/tenants/{tenant_id}/domains/{domain_id}

GET    /admin/tenants/{tenant_id}/entitlements
PUT    /admin/tenants/{tenant_id}/entitlements
POST   /admin/tenants/{tenant_id}/entitlements/validate
DELETE /admin/tenants/{tenant_id}/entitlements

GET    /admin/tenants/{tenant_id}/execution-config
PUT    /admin/tenants/{tenant_id}/execution-config
POST   /admin/tenants/{tenant_id}/execution-config/validate
DELETE /admin/tenants/{tenant_id}/execution-config
```

The compatibility endpoint for listing execution-config tenants remains available:

```text
GET /admin/execution-config-tenants
```

Existing admin thread-inspection and audit-listing endpoints remain tenant-scoped under
`/admin/tenants/{tenant_id}`.

## Storage and caching

The durable SQLite admin store is the current source of truth for local/single-node tenant
registry data, tenant domains, entitlements, and store-backed tenant execution configs.

Implemented caching/invalidation:

- Store-backed execution config resolution uses an in-process cache.
- Admin execution-config writes and deletes invalidate the in-process execution resolver for
  the affected tenant.

Not yet implemented:

- Tenant registry record caching.
- Tenant entitlement caching.
- Registry/entitlement cache invalidation.
- Cross-process cache invalidation for multi-instance deployments.

Because registry and entitlements are currently loaded from the store during tenant-context
resolution, stale registry/entitlement cache behavior is not a current runtime concern. It
will become relevant if registry or entitlement caching is added.

## Security requirements and status

- Do not trust arbitrary tenant IDs from request headers unless the active auth mode is a
  development-only mode or the token is otherwise verified.
- Verify that the caller is authorized for the target tenant before exposing tenant data.
- Require `is_admin=true` for control-plane operations. More granular admin roles remain
  future work.
- Audit all tenant mutations with actor, timestamp, action, old values, and new values.
  Current audit coverage is partial: tenant/domain/entitlement mutations are audited, while
  execution-config mutation audit coverage should be completed.
- Redact secrets from read responses and audit records. Execution-config read responses and
  audit helper payloads currently redact secret-looking values.
- Validate slug and domain uniqueness.
- Prevent tenant enumeration through error messages where public routes are involved. Admin
  routes can return specific not-found details.
- Keep data access tenant-scoped at the storage layer, not only in route handlers.

## Migration path and current status

### Phase 1: Inventory current static config

Status: ongoing / not directly represented in code.

List every tenant-specific setting currently represented by env vars, static JSON, or
runtime assumptions. Classify each as identity, status, entitlement, execution config,
secret, or operational metadata.

### Phase 2: Add tenant registry schema

Status: implemented.

Durable tenant, domain, entitlement, and execution-config tables exist in the SQLite admin
store. Audit records are stored in the thread store rather than the admin store.

### Phase 3: Read registry state during requests

Status: implemented with compatibility fallback.

Registry state, entitlements, and execution-config version are loaded during request-time
tenant-context resolution when the admin store is enabled. Active-tenant enforcement is
opt-in with `MINIGENT_TENANT_REGISTRY_REQUIRED`.

### Phase 4: Add admin tenant APIs

Status: mostly implemented.

Tenant lifecycle, domain, entitlement, execution-config, seed, validation, and listing APIs
exist. CLI support exists for tenant lifecycle and entitlements. Domain CLI support should be
verified or added if needed by operations workflows.

### Phase 5: Move tenant-specific defaults out of env

Status: incomplete / ongoing.

Keep static config for global defaults and bootstrap behavior only. Make tenant registry,
entitlements, and execution config the runtime source of truth for tenant-specific state.

## Remaining follow-up work

- Resolve tenant from verified request domain/host where that deployment mode is enabled.
- Add broader quota/rate-limit enforcement.
- Add registry and entitlement caching only if needed, with per-tenant invalidation and a
  multi-instance invalidation mechanism.
- Add granular admin roles beyond `is_admin=true`.
- Audit execution-config create/update/delete operations.
- Decide whether entitlement writes and execution-config writes must require an existing
  registry tenant after migration.
- Add or enforce foreign-key constraints once registry-first operation is mandatory.
- Decide whether plan-derived entitlements should be snapshots, computed defaults,
  explicit overrides, or a combination.
- Clarify which tenant states should block reads, writes, new runs, and admin-only access.
- Complete migration of tenant-specific defaults from env/static config to durable tenant
  records and execution config.

## Open questions

- When should compatibility mode end for execution-config-only tenant IDs?
- Should slug changes remain freely allowed, or should they require aliases, redirects, or a
  deprecation period?
- Which tenant states should block thread reads versus only message creation and new runs?
- Should entitlements be explicit overrides, plan-derived snapshots, computed from plan at
  request time, or both?
- What admin role model is needed beyond the current `is_admin=true` flag?
- When multiple API instances are deployed, what cache-invalidation mechanism should be
  used?
