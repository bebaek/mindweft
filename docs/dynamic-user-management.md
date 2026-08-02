# Dynamic user management

Status: Partially implemented

Implemented: tenant membership model and SQLite store, global-admin and tenant-owner scoped
CRUD/list/status-transition APIs and UI, last-active-owner and self-credential lockout protection,
tenant-scoped encrypted OpenAI OAuth import from Pi, mutation audit records, optional request-time
active-membership enforcement with `MINIGENT_TENANT_USER_REGISTRY_REQUIRED`, and membership fields
on `TenantContext`.

Still pending or partial: invite-token/email delivery workflows, granular tenant-admin RBAC, richer
identity-provider mapping, service-account modeling, and seat/billing limits.

Focus: tenant user and membership management, not full identity management.

Minigent already has tenant-scoped authentication, tenant registry records, admin APIs,
audit records, tenant context resolution, and tenant-scoped thread storage. Basic dynamic
user management should build on that foundation by adding durable tenant membership state
that can be managed at runtime and optionally enforced during request-time tenant context
resolution.

## Context

The current auth model identifies a principal with a tenant ID, user ID, and admin flag. That
is enough for local development and trusted deployments, but it does not provide a durable
record of which users belong to a tenant, which users are suspended, or which role a user has
inside a tenant.

Dynamic tenant management already introduced the registry/control-plane pattern for tenants.
Dynamic user management should use the same pattern for tenant memberships while avoiding
full identity-provider scope in the first iteration.

## Goals

- Manage tenant membership at runtime without deployment-driven config edits.
- Support active, invited, suspended, and deleted user states.
- Support simple tenant roles for support and operations workflows.
- Audit all membership mutations.
- Preserve current auth behavior during migration.
- Optionally require active tenant membership before business logic runs.
- Keep identity-provider concerns decoupled from Minigent membership state.

## Non-goals for the first iteration

- Password management.
- Public signup flows.
- Email invite delivery.
- General OAuth account linking beyond the tenant-owner Pi credential import.
- SCIM provisioning.
- Full organization/team hierarchy.
- Billing integration and seat enforcement.
- Fine-grained per-route RBAC beyond basic role metadata.

These can be layered on later. The first version should be a tenant membership registry.

## Terminology

- **Identity**: the externally authenticated person or service account. Minigent should not
  own passwords or external identity lifecycle in the first iteration.
- **User ID**: stable identifier from authenticated principal material or an admin-created
  membership record.
- **Tenant membership**: a durable record that a user belongs to a tenant with a role and
  status.
- **Role**: coarse tenant-level authorization label, initially `owner`, `admin`, `member`,
  or `viewer`.
- **Status**: lifecycle state for a membership: `invited`, `active`, `suspended`, or
  `deleted`.

## Implemented data model

The SQLite admin store includes a membership table alongside the tenant registry:

```text
tenant_users
  id
  tenant_id
  user_id
  email
  display_name
  role: owner | admin | member | viewer
  status: invited | active | suspended | deleted
  metadata_json
  created_by
  updated_by
  created_at
  updated_at
```

Recommended indexes and constraints:

```text
UNIQUE (tenant_id, user_id)
INDEX  (tenant_id, status)
INDEX  (tenant_id, role)
INDEX  (email)
```

If the admin store later enforces foreign keys, `tenant_users.tenant_id` should reference
`tenants.id`. During migration, the implementation may initially tolerate membership records
for tenant IDs that exist only in execution-config compatibility paths, but registry-first
operation should eventually require a tenant registry row.

## Request-time behavior

Current request-time membership enforcement preserves existing auth behavior unless
membership enforcement is explicitly enabled.

Current request flow when the admin store is available:

1. Authenticate the caller and build the principal.
2. Resolve tenant context from the tenant registry as today.
3. If user-registry enforcement is enabled, load the membership for
   `(tenant_id, principal.user_id)`.
4. Reject missing or non-`active` memberships before business logic runs.
5. Attach membership fields to `TenantContext`.
6. Continue with execution config, entitlements, and tenant-scoped storage.

Opt-in flag:

```text
MINIGENT_TENANT_USER_REGISTRY_REQUIRED=true
```

When disabled, Minigent should continue trusting authenticated principal material for user
identity and tenant ID. This keeps local development and existing trusted deployments working
while operators migrate membership data.

## Tenant/user context

The current implementation extends `TenantContext` with optional membership fields:

```text
membership_id
membership_email
membership_display_name
user_role
user_status
membership_metadata
```

These fields remain optional for compatibility mode. Active membership metadata is included whenever
a matching record exists; when `MINIGENT_TENANT_USER_REGISTRY_REQUIRED=true`, missing or inactive
membership is rejected instead of being omitted.

## Admin operations

Initial admin operations should include:

| Operation | Notes |
| --- | --- |
| List tenant users | Filter by status, role, email/user search, limit, offset. |
| Create tenant user | Create membership with role and status. |
| Read tenant user | Fetch one membership by membership ID or user ID. |
| Update tenant user | Update email, display name, role, status, metadata. |
| Activate tenant user | Transition to `active`. |
| Suspend tenant user | Transition to `suspended`. |
| Delete tenant user | Soft delete by status transition to `deleted`. |
| Invite tenant user | Optional first-iteration alias for creating `invited` status; no email delivery. |

Every mutating operation should append an audit record with actor, action, old values, new
values, resource type, resource ID, and tenant ID.

## API sketch

Suggested admin routes:

```text
GET    /admin/tenants/{tenant_id}/users
POST   /admin/tenants/{tenant_id}/users
GET    /admin/tenants/{tenant_id}/users/{user_id}
PATCH  /admin/tenants/{tenant_id}/users/{user_id}
POST   /admin/tenants/{tenant_id}/users/{user_id}/activate
POST   /admin/tenants/{tenant_id}/users/{user_id}/suspend
DELETE /admin/tenants/{tenant_id}/users/{user_id}
```

Optional invite alias:

```text
POST /admin/tenants/{tenant_id}/users/invite
```

If both membership ID and user ID lookups are needed, prefer explicit paths to avoid
ambiguity:

```text
GET /admin/tenants/{tenant_id}/users/by-user-id/{user_id}
GET /admin/tenants/{tenant_id}/users/{membership_id}
```

## Role model

Initial roles should be simple tenant-level labels:

```text
owner
admin
member
viewer
```

Suggested semantics:

- `owner`: tenant-level superuser; future owner-only actions can use this role.
- `admin`: tenant administrator; can manage tenant users if granular tenant-admin APIs are
  added later.
- `member`: normal user.
- `viewer`: read-oriented user; enforcement can be added later.

The first implementation can store roles without enforcing detailed RBAC. Request-time
membership enforcement should initially focus on membership status, not full permission
matrices.

## Security requirements

- Do not use tenant membership records as authentication credentials.
- Keep authentication delegated to the existing auth modes and external identity providers.
- Treat membership as authorization and lifecycle metadata.
- Require global admin access for cross-tenant operations. Active `owner` memberships may use the
  explicitly delegated tenant-scoped profile, member, credential, domain, entitlement-read, and
  execution-configuration routes only for their own tenant.
- Tenant owners cannot change plan/region/metadata, verify domains, perform lifecycle operations,
  remove or demote the final active owner, or disable their own local credential.
- Audit every membership mutation.
- Redact secrets from membership metadata in read responses and audit records.
- Normalize and validate email addresses if email is provided.
- Prevent membership enumeration on public routes. Admin routes may return specific
  not-found details.
- Avoid silently reactivating deleted or suspended users through duplicate create requests;
  require explicit status transition or update behavior.

## Migration path

### Phase 1: Add membership schema

Add the `tenant_users` table, model types, validation helpers, and store methods. Keep all
request-time behavior unchanged.

### Phase 2: Add admin membership APIs

Add CRUD, status-transition, list, and audit behavior under
`/admin/tenants/{tenant_id}/users`. Start with global admin authorization.

### Phase 3: Add CLI support

Status: implemented.

CLI commands exist for list/show/create/update/activate/suspend/delete membership workflows.

### Phase 4: Add optional request-time enforcement

Status: implemented.

`MINIGENT_TENANT_USER_REGISTRY_REQUIRED` rejects requests from principals without an active
tenant membership when enabled.

### Phase 5: Introduce granular roles

Use stored roles for tenant-admin workflows and route-level permissions only after the basic
membership registry is stable.

## Open questions

- Should membership lookup use principal `user_id`, email, subject claim, or a configurable
  external identity key?
- Should `email` be required, optional, or only metadata?
- Should `owner` have special invariants, such as at least one active owner per tenant?
- Should deleted memberships block recreation with the same `(tenant_id, user_id)`, or should
  create reactivate soft-deleted records only with an explicit flag?
- Should tenant admins be allowed to manage users, or should first-iteration APIs remain
  global-admin-only?
- Should membership status affect all routes, or only write/run routes?
- How should service accounts be represented: as users, a separate table, or role metadata?
