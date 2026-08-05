# Tenant registry bootstrap from execution configuration

## Purpose

Minigent has two related but independent sources of tenant state:

- execution configuration, which controls runtime LLM, tool, and MCP behavior; and
- the durable admin tenant registry, which controls mutable metadata, domains,
  entitlements, and administrative ownership.

A tenant can therefore have an execution configuration without having a row in the
admin store. For example, a tenant declared by `MINIGENT_TENANT_EXECUTION_CONFIGS`
can resolve execution successfully while admin metadata mutations correctly return
`404` because the tenant is not registered.

The bootstrap workflow will make this state visible and provide an explicit,
idempotent way to create the missing registry records. It must not silently change
execution configuration or create tenants during application startup.

## Discovery

Add a shared discovery service used by both the admin API and CLI. It should inspect
store-backed and deployment-backed execution configuration and return, for each
configured tenant:

```json
{
  "tenant_id": "demo-tenant",
  "source": "environment",
  "registry_present": false,
  "execution_config_present": true
}
```

Discovery must report existing records, missing records, reserved platform-admin
configuration, and ID/slug conflicts without modifying state.

## Seed behavior

Extend the existing execution-config seed workflow with the unified discovery source.
The operation must support:

- dry-run mode;
- explicit tenant selection;
- configurable default status, plan, and region;
- idempotent reruns;
- conflict reporting;
- exclusion of the reserved platform-admin execution configuration.

Defaults for a missing record are deterministic:

- `id`: execution-config tenant ID;
- `slug`: normalized tenant ID;
- `name`: tenant ID.

Existing registry metadata and execution configuration must never be overwritten.

Example dry-run result:

```json
{
  "source": "execution-configs",
  "dry_run": true,
  "discovered": 1,
  "missing_registry_records": 1,
  "tenants": [
    {
      "id": "demo-tenant",
      "action": "would_create",
      "execution_config_source": "environment"
    }
  ]
}
```

## Interfaces

Keep the API operation explicit and admin-authorized. Add a matching CLI command:

```bash
minigent admin tenants seed --from execution-configs --dry-run
minigent admin tenants seed --from execution-configs \
  --status active --plan pro --region demo

# Resolve a known derived-slug collision explicitly:
minigent admin tenants seed --from execution-configs \
  --tenant demo-tenant \
  --slug-override demo-tenant=demo-primary \
  --conflict-policy fail
```

Do not run this migration automatically in application startup. Operators should be
able to inspect the dry-run output before creating metadata records.

## Audit and tests

Each created registry record must emit an audit event containing the actor, source,
defaults, and new tenant metadata. Tests must cover environment-only tenants,
store-backed tenants, mixed sources, dry-run behavior, idempotence, conflicts,
reserved IDs, audit records, and preservation of execution configuration.

## Operational sequence

1. Deploy discovery in read-only/dry-run mode.
2. Inspect missing registry records in the admin API or CLI.
3. Run the explicit seed operation for approved tenant IDs.
4. Verify tenant metadata, domains, entitlements, and audit records.
5. Use confirmed admin-chat mutations only after registry bootstrap completes.
