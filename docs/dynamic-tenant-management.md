# Dynamic tenant management

Status: Partially implemented

Initial implementation includes a SQLite-backed tenant registry, admin tenant lifecycle
endpoints and CLI commands, soft deletion via tenant status, an execution-config tenant
listing compatibility endpoint, execution-config tenant seeding, tenant entitlement storage,
request-time tenant context resolution, structured tenant audit metadata, opt-in
request-time active-tenant enforcement with `MINIGENT_TENANT_REGISTRY_REQUIRED`, and initial
runtime entitlement enforcement for `peer_agents`/`mcp` feature flags plus `max_threads`,
`max_messages_per_thread`, and `max_thread_runs` limits, tenant-context exposure of the
exact tenant execution config version, admin-managed tenant domain registration with manual
verification, and admin lookup of domains to tenant IDs. Request routing by tenant domain,
broader quota coverage, and cache invalidation remain proposed follow-up work.

## Context

Minigent already supports tenant-scoped authentication, thread isolation, per-tenant
execution config, and an optional admin SQLite store for execution config. The current
tenant model is still mostly config-oriented: tenants are identified by authenticated
requests, and execution resources can be loaded from `MINIGENT_TENANT_EXECUTION_CONFIGS`
or from the admin store.

That shape works for local development and a small number of tenants, but static tenant
configuration does not scale well. Adding, suspending, renaming, changing limits, or
changing tenant-specific capabilities should not require editing environment variables or
redeploying the service.

Dynamic tenant management should make tenants runtime-managed entities with durable state,
admin APIs, audit records, validation, and cache invalidation.

## Goals

- Manage tenant lifecycle at runtime without deployment-driven config edits.
- Keep request-time tenant resolution and authorization explicit and auditable.
- Preserve existing tenant thread isolation and execution-config behavior during migration.
- Support per-tenant status, domains/slugs, plan/entitlements, quotas, and execution config.
- Provide admin APIs and CLI/UI surfaces for safe support and operations workflows.
- Keep global defaults static while tenant-specific state lives in a durable store.

## Non-goals for the first iteration

- Full billing integration.
- Automated custom-domain certificate provisioning.
- Multi-region tenant migration.
- Per-tenant database provisioning.
- Self-service public signup flows.

Those can build on the same registry later, but the first version should focus on the
control-plane foundation.

## Terminology

- **Tenant ID**: immutable internal identifier used for authorization, thread isolation,
  audit records, and foreign keys.
- **Slug**: unique, URL-friendly human identifier such as `acme` or `big-river-labs`.
  Slugs may be visible in admin tools, path-based tenant URLs, or subdomains.
- **Tenant registry**: durable source of truth for tenant identity, state, domains, and
  operational metadata.
- **Execution config**: tenant-specific LLM, tool, MCP, skill, capability-profile,
  backend, and quality configuration.
- **Entitlements**: plan-derived or overridden features and limits that determine what a
  tenant is allowed to use.

## Proposed model

Add a tenant registry alongside the existing execution-config store. The registry owns
identity and lifecycle state; execution config remains a separate but related document.

Suggested first-class tenant fields:

```text
id
slug
name
status: active | provisioning | suspended | archived | deleted
plan
region
created_at
updated_at
created_by
updated_by
metadata
```

Related records:

```text
tenant_domains
  id
  tenant_id
  domain
  verified
  created_at

tenant_entitlements
  tenant_id
  features
  limits
  version
  updated_at

tenant_execution_configs
  tenant_id
  config
  version
  updated_at
```

Important attributes such as `status`, `slug`, `plan`, and `region` should be columns, not
only JSON fields, because they are used for routing, filtering, authorization, and support
workflows. Flexible metadata can stay in JSON.

## Request-time behavior

A typical request should follow this flow:

1. Authenticate the caller.
2. Resolve or read the tenant ID from trusted auth material.
3. Load tenant registry state from cache or durable store.
4. Reject inactive tenants before running business logic.
5. Verify user membership or token authority for that tenant.
6. Attach tenant context to the request and downstream run state.
7. Load execution config and entitlements for the tenant.
8. Run the thread operation with tenant-scoped storage and policy.

Tenant context should include at least:

```text
tenant_id
slug
status
plan
region
feature flags
limits
execution_config_version
entitlements_version
```

The context must be available to HTTP handlers, background jobs, event consumers, logging,
metrics, and audit writing. Avoid designs where only HTTP middleware knows the tenant.

## Admin operations

The admin control plane should eventually support:

- Create tenant.
- Update tenant name, slug, plan, metadata, and region.
- Activate, suspend, archive, or delete tenant.
- Add, verify, or remove tenant domains.
- Read and update entitlements.
- Read, validate, update, or delete execution config.
- List tenants with filters for status, plan, slug, and updated time.
- Emit audit records for every mutating operation.

Sensitive operations should support validation or dry-run behavior where practical. For
example, changing a slug should check uniqueness and show affected routes before commit.

## API sketch

Initial endpoints could extend the existing admin surface:

```text
GET    /admin/tenants
POST   /admin/tenants
GET    /admin/tenants/{tenant_id}
PATCH  /admin/tenants/{tenant_id}
POST   /admin/tenants/{tenant_id}/activate
POST   /admin/tenants/{tenant_id}/suspend
POST   /admin/tenants/{tenant_id}/archive
DELETE /admin/tenants/{tenant_id}

GET    /admin/tenants/{tenant_id}/domains
POST   /admin/tenants/{tenant_id}/domains
DELETE /admin/tenants/{tenant_id}/domains/{domain_id}

GET    /admin/tenants/{tenant_id}/entitlements
PUT    /admin/tenants/{tenant_id}/entitlements
POST   /admin/tenants/{tenant_id}/entitlements/validate
```

Existing execution-config and thread-inspection endpoints can remain tenant-scoped under
`/admin/tenants/{tenant_id}`.

## Storage and caching

Use a durable store as the source of truth. The current admin SQLite store is a reasonable
local/single-node starting point. If Minigent later needs multiple API instances, use a
shared database and cross-process cache invalidation.

Recommended cache behavior:

- Cache tenant registry, entitlements, and execution config separately.
- Include version fields in cached payloads.
- Invalidate per-tenant cache entries on admin writes.
- Keep TTLs short enough that stale policy is bounded if invalidation fails.
- Fail closed for missing tenants when the configured source of truth is the store.

Execution config already has in-process cache invalidation on admin writes. Tenant registry
and entitlement caches should follow the same pattern.

## Security requirements

- Do not trust arbitrary tenant IDs from request headers unless the active auth mode is a
  development-only mode or the token is otherwise verified.
- Verify that the caller is authorized for the target tenant before exposing tenant data.
- Require `is_admin=true` or a more granular admin role for control-plane operations.
- Audit all tenant mutations with actor, timestamp, action, old values, and new values.
- Redact secrets from read responses and audit records.
- Validate slug and domain uniqueness.
- Prevent tenant enumeration through error messages where public routes are involved.
- Keep data access tenant-scoped at the storage layer, not only in route handlers.

## Migration path

### Phase 1: Inventory current static config

List every tenant-specific setting currently represented by env vars, static JSON, or
runtime assumptions. Classify each as identity, status, entitlement, execution config,
secret, or operational metadata.

### Phase 2: Add tenant registry schema

Add durable tenant, domain, entitlement, and audit tables. Seed tenants from existing
execution config keys and known auth-token tenant IDs where appropriate.

### Phase 3: Read registry state during requests

Keep existing execution-config behavior, but require a known active tenant when the tenant
config source is store-backed. Continue allowing development defaults for local workflows.

### Phase 4: Add admin tenant APIs

Add create/read/update/list/status-transition operations with validation and audit records.
Expose CLI commands after the HTTP API stabilizes.

### Phase 5: Move tenant-specific defaults out of env

Keep static config for global defaults and bootstrap behavior only. Make tenant registry,
entitlements, and execution config the runtime source of truth for tenant-specific state.

## Open questions

- Should tenant IDs be generated UUIDs only, or should existing string IDs remain valid for
  compatibility?
- Should slug changes be allowed after tenant creation, or should they require an alias or
  redirect period?
- Which tenant states should block thread reads versus only block new runs?
- Should entitlements be stored as explicit overrides, plan-derived snapshots, or both?
- What admin role model is needed beyond the current `is_admin=true` flag?
- When multiple API instances are deployed, what cache-invalidation mechanism should be
  used?
