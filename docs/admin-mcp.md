# Admin operations MCP

Minigent exposes a read-only Streamable HTTP MCP v2 endpoint at `/mcp`. It runs in the
same FastAPI process as the REST and admin APIs, and is intended for an authenticated platform
administrator or an administrator-operated agent to diagnose a complicated deployment safely.

## Authentication and authorization

The endpoint uses the normal Minigent authentication configuration. Every MCP request is
authenticated independently, and every tool requires a principal with `is_admin: true`. It does
not accept a separate MCP-only credential and it does not use a privileged loopback call to the
REST API.

For a development-header deployment, a client must send:

```text
X-Minigent-User-Id: admin-1
X-Minigent-Tenant-Id: tenant-1
X-Minigent-Admin: true
```

Static-token and JWT deployments use their normal `Authorization: Bearer ...` authentication.
Production deployments should use static tokens or JWT rather than development headers.

## Tools

All tools are read-only and return purpose-built redacted summaries. They never return stored
secret values, bearer tokens, credential headers, or configured MCP URLs.

- `get_setup_status` reports deployment readiness, whether durable admin configuration exists,
  configured MCP catalog count, and actionable non-secret findings.
- `diagnose_tenant_setup(tenant_id)` reports effective execution resolution and tenant MCP policy
  and assignment readiness.
- `list_mcp_server_catalog_access(tenant_id, user_id)` reports the catalog entries effectively
  available to a tenant user, including only server ID, name, transport category, and allowed tool
  names.

## Admin chat

Authenticated platform-admin chat runs can invoke these operations directly in process; no
loopback HTTP request and no forwarding of the user's bearer or session credential is involved.
The runtime composes a role-scoped tool registry with the thread's normal tool registry after its
skill, capability-profile, and MCP access policy have been resolved. This composition point can
later supply purpose-built tools for tenant owners or other roles without granting those roles the
platform-admin surface.

The chat-visible names are deliberately namespaced so they cannot be confused with ordinary
tenant tools:

- `minigent_admin_get_setup_status`
- `minigent_admin_diagnose_tenant_setup`
- `minigent_admin_list_mcp_server_catalog_access`

These tools are present only when the principal has `is_admin: true`. The externally callable MCP
names remain unchanged for compatibility.

This first surface deliberately cannot modify tenant execution config, MCP catalog policies,
assignments, credentials, path policy, or shell access. A later mutation surface must use an
explicit preview plus a tenant-bound, single-use confirmation flow.

## Client configuration

Point an MCP v2 Streamable HTTP client at the Minigent deployment's `/mcp` path and configure the
same bearer authentication it uses for the Minigent API. The endpoint supports modern MCP v2
discovery and tool calls over stateless Streamable HTTP.
