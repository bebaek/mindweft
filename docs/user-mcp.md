# User MCP

Minigent exposes a Streamable HTTP MCP v2 endpoint at `/user-mcp` for an authenticated active
tenant user. It is separate from the administrator-only `/mcp` endpoint.

## Authentication and scope

The endpoint uses the normal Minigent authentication configuration. A request must authenticate
as an active tenant user. The MCP tools derive `tenant_id` and `user_id` from that authenticated
principal; they do not accept user or tenant selectors.

Development-header deployments can send:

```text
X-Minigent-User-Id: user-1
X-Minigent-Tenant-Id: tenant-1
```

Static-token and JWT deployments use their normal `Authorization: Bearer ...` authentication.
Administrator principals should use `/mcp` for platform operations instead.

## Tools

The first user MCP surface is read-only:

- `get_user_execution_status` reports whether the caller has personal execution configuration,
  its version, resource counts, encrypted credential availability, and safe findings.
- `get_user_execution_config` returns the caller's normalized personal execution configuration
  with API keys and reusable headers redacted.
- `validate_user_execution_config(config)` validates and normalizes a proposed personal config
  without storing it.
- `list_user_mcp_access` reports the caller's personal and tenant-shared MCP servers, effective
  policy for personal servers, allowed tool names, and whether a personal credential reference is
  configured.
- `put_user_execution_config(config, expected_version)` validates and stores the caller's personal
  execution config using optimistic concurrency.
- `delete_user_execution_config(confirm, expected_version)` deletes the caller's config only when
  explicitly confirmed.
- `put_user_execution_credential(credential_ref, header_name, header_value, expected_version)`
  stores an encrypted write-only credential and returns metadata only.
- `delete_user_execution_credential(credential_ref, confirm, expected_version)` deletes an
  encrypted credential only when explicitly confirmed.

Tools never return credential values, authorization headers, or another user's configuration.
The effective access view reuses the same tenant policy and user execution catalog used by runtime
execution.

The first user MCP surface also supports user-owned configuration and credential mutations.
Mutations are principal-scoped, use optimistic version checks, and return metadata rather than
secret values.

## Client configuration

Point a Streamable HTTP MCP v2 client at the deployment's `/user-mcp` path and configure the same
bearer authentication used for the Minigent API. The endpoint supports modern MCP discovery and
tool calls over stateless Streamable HTTP.
