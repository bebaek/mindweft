# Minigent

Minimal AI agent runtime POC from `DESIGN.md`.

## What it includes

- FastAPI service
- In-memory thread/message store
- Simple agent execution loop
- Pluggable tool registry
- Replaceable LLM adapter boundary
- OpenAI and OpenRouter support via one OpenAI-compatible adapter
- Optional MCP tool discovery and invocation over HTTP
- Deterministic mock adapter for local testing

## Run

```bash
uv venv
source .venv/bin/activate
env UV_CACHE_DIR=.uv-cache uv sync --dev
env UV_CACHE_DIR=.uv-cache uv run uvicorn app.main:app --reload
```

You can put provider settings in a local `.env` file. Start from [.env.example](/Users/burm/code/minigent/.env.example).

## Authentication

Authentication is controlled by `MINIGENT_AUTH_MODE`:

- `dev-headers`: trust `X-Minigent-*` headers for local development
- `static-tokens`: resolve bearer tokens from `MINIGENT_AUTH_TOKENS`
- `jwt`: verify bearer JWTs and map claims into a `Principal`

### JWT Mode

Production-oriented mode uses JWT verification:

```dotenv
MINIGENT_AUTH_MODE=jwt
MINIGENT_JWT_ISSUER=https://issuer.example
MINIGENT_JWT_AUDIENCE=minigent-api
MINIGENT_JWT_ALGORITHMS=["RS256"]
MINIGENT_JWT_JWKS_URL=https://issuer.example/.well-known/jwks.json
```

For local JWT testing you can use `HS256`:

```dotenv
MINIGENT_AUTH_MODE=jwt
MINIGENT_JWT_ISSUER=minigent-dev
MINIGENT_JWT_AUDIENCE=minigent-api
MINIGENT_JWT_ALGORITHMS=["HS256"]
MINIGENT_JWT_SHARED_SECRET=replace-with-a-long-dev-secret
```

JWT claim mapping defaults to:

```text
sub -> user_id
tenant_id -> tenant_id
is_admin -> is_admin
```

### Static Token Mode

Static bearer-token auth is still available for simple environments:

```dotenv
MINIGENT_AUTH_MODE=static-tokens
MINIGENT_AUTH_TOKENS={"dev-token":{"user_id":"demo-user","tenant_id":"demo-tenant","is_admin":false}}
```

Send that token with:

```bash
Authorization: Bearer dev-token
```

### Development Header Mode

For local development without token verification:

```bash
MINIGENT_AUTH_MODE=dev-headers
X-Minigent-User-Id: user-123
X-Minigent-Tenant-Id: tenant-abc
X-Minigent-Admin: false
```

Thread lifecycle endpoints require the auth material for the active mode. Threads are isolated by `tenant_id`, and cross-tenant access returns `404`.

## Tenant Execution Config

The runtime config source is controlled by `MINIGENT_TENANT_CONFIG_SOURCE`:

- `env`: use only env-based execution config
- `store`: use only the admin SQLite store and fail closed when a tenant has no config
- `store-with-defaults`: use the admin store first, then fall back to env/default resolver

Execution resources can be scoped per tenant with `MINIGENT_TENANT_EXECUTION_CONFIGS`:

```dotenv
MINIGENT_TENANT_EXECUTION_CONFIGS={
  "tenant-1":{
    "llm":{"provider":"mock"},
    "tools":{"allowed_local_tools":["echo","current_time"]}
  },
  "tenant-2":{
    "llm":{
      "provider":"openai",
      "model":"gpt-5.4-mini",
      "api_key":"tenant-2-key"
    },
    "tools":{"allowed_local_tools":["calculator"]}
  }
}
```

Supported fields:

- `llm.provider`: `mock`, `openai`, `openrouter`, or `openai-compatible`
- `llm.model`, `llm.base_url`, `llm.api_key`, `llm.extra_headers`, `llm.timeout`
- `tools.allowed_local_tools`: local tool allowlist
- `tools.mcp_servers`: per-tenant MCP server definitions

The local tool `retrieve_knowledge` is available when Minigent is run with the `minirag`
extra installed and `MINIGENT_MINIRAG_DB_PATH` set to a SQLite database created by
`minirag ingest`.

For local development with `uv`, install it with:

```bash
uv sync --extra minirag
```

In `store-with-defaults`, a `*` tenant record in the admin store acts as a default profile before env fallback is considered.

## Admin API

The admin API is an optional control plane for tenant execution config backed by SQLite.

Enable it with:

```dotenv
MINIGENT_ADMIN_DB_PATH=.data/minigent-admin.db
MINIGENT_ADMIN_ENCRYPTION_KEY=replace-with-a-long-random-secret
```

Admin endpoints:

- `GET /admin/tenants`
- `GET /admin/tenants/{tenant_id}/execution-config`
- `PUT /admin/tenants/{tenant_id}/execution-config`
- `POST /admin/tenants/{tenant_id}/execution-config/validate`
- `DELETE /admin/tenants/{tenant_id}/execution-config`

Admin access requires an authenticated principal with `is_admin=true`. In `dev-headers` mode that means:

```bash
X-Minigent-User-Id: admin-user
X-Minigent-Tenant-Id: admin-tenant
X-Minigent-Admin: true
```

Secrets such as LLM API keys and MCP headers are accepted on writes but redacted in read responses. If `MINIGENT_TENANT_CONFIG_SOURCE` is `store` or `store-with-defaults`, `MINIGENT_ADMIN_ENCRYPTION_KEY` is required and those secrets are encrypted before being written to SQLite. Updating or deleting a tenant config invalidates the in-process execution cache for that tenant so new runs pick up the change immediately.

`POST /admin/tenants/{tenant_id}/execution-config/validate` accepts the same payload shape as `PUT` and returns a structured preflight report covering config shape, LLM wiring, local tool policy, and MCP connectivity without persisting the config.

## Provider Config

`mock` remains the default, so the service starts without credentials.

OpenAI:

```bash
MINIGENT_LLM_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.4-mini
```

OpenRouter:

```bash
MINIGENT_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=openai/gpt-5.4-mini
OPENROUTER_HTTP_REFERER=https://your-app.example
OPENROUTER_APP_NAME=minigent
```

Optional overrides:

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

## MCP Config

You can attach HTTP MCP servers by setting `MINIGENT_MCP_SERVERS` to a JSON array in `.env`.

Example:

```dotenv
MINIGENT_MCP_SERVERS=[{"name":"demo","url":"https://example.com/mcp","headers":{"Authorization":"Bearer token"}}]
```

Discovered MCP tools are namespaced as `<server>.<tool>`, for example `demo.echo`.

Current scope:
- `initialize`
- `notifications/initialized`
- `tools/list`
- `tools/call`

The service skips MCP servers that fail during startup and exposes connected servers in `/config`.

## Observability

Logging defaults to plaintext. Set `MINIGENT_LOG_FORMAT=json` for structured logs.

Useful logging env vars:

```bash
MINIGENT_LOG_LEVEL=INFO
MINIGENT_LOG_FORMAT=plaintext
MINIGENT_LOG_PLAINTEXT_FORMAT=%(levelname)s %(name)s: %(message)s
MINIGENT_LOG_JSON_ROOT_KEY=log
MINIGENT_LOG_JSON_MESSAGE_KEY=message
MINIGENT_LOG_JSON_LEVEL_KEY=level
MINIGENT_LOG_JSON_LOGGER_KEY=logger
MINIGENT_LOG_JSON_TIMESTAMP_KEY=timestamp
MINIGENT_LOG_JSON_EXCEPTION_KEY=exception
MINIGENT_LOG_JSON_FIELDS={"service":"minigent","env":"dev"}
MINIGENT_LOG_JSON_INCLUDE_TRACE_CONTEXT=true
```

OpenTelemetry tracing is optional:

```bash
MINIGENT_OTEL_ENABLED=true
MINIGENT_OTEL_SERVICE_NAME=minigent
MINIGENT_OTEL_EXPORTER=console
MINIGENT_OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.example/v1/traces
MINIGENT_OTEL_EXPORTER_OTLP_HEADERS={"authorization":"Bearer token"}
MINIGENT_OTEL_EXPORTER_OTLP_TIMEOUT_SECONDS=10
```

Set `MINIGENT_OTEL_EXPORTER=none` to keep trace context active without exporting spans.

## Test

```bash
env UV_CACHE_DIR=.uv-cache uv run pytest
```

## Development

Lint:

```bash
env UV_CACHE_DIR=.uv-cache uv run ruff check .
```

Format:

```bash
env UV_CACHE_DIR=.uv-cache uv run ruff format .
```

Type check:

```bash
env UV_CACHE_DIR=.uv-cache uv run pyright
```

## Contributing

Use Conventional Commits for commit messages.

Use Ruff for linting and formatting. Bugbear checks run through Ruff's `B` ruleset. Use Pyright for type checking in `app/`.

Example:

```text
chore: redact secrets from MCP URL logging
```

## Demo Client

With the server running, you can drive it with [scripts/demo_client.py](/Users/burm/code/minigent/scripts/demo_client.py):

```bash
env UV_CACHE_DIR=.uv-cache uv run python scripts/demo_client.py "hello"
env UV_CACHE_DIR=.uv-cache uv run python scripts/demo_client.py "/tool echo hello from tool"
env UV_CACHE_DIR=.uv-cache uv run python scripts/demo_client.py "/tool current_time"
```

To continue an existing thread:

```bash
env UV_CACHE_DIR=.uv-cache uv run python scripts/demo_client.py --thread-id <thread_id> "follow up"
```

To create a thread with a specific skill:

```bash
env UV_CACHE_DIR=.uv-cache uv run python scripts/demo_client.py --skill-name math "/tool echo blocked by skill"
```

## Skills Demo

Skills are execution overlays, not independent permission grants. Tenant tool config defines the
maximum available tools and MCP servers. When a thread activates a skill, the runtime narrows the
effective tool and MCP access to the intersection of the tenant configuration and the skill
allowlists. Skills can reduce access for a thread, but they cannot expand access beyond the tenant
configuration.

Use this tenant config with the mock adapter to demo default and explicit skills:

```dotenv
MINIGENT_AUTH_MODE=dev-headers
MINIGENT_TENANT_EXECUTION_CONFIGS={
  "demo-tenant":{
    "llm":{"provider":"mock"},
    "tools":{"allowed_local_tools":["echo","calculator","current_time"]},
    "skills":{
      "default_skill":"support",
      "items":[
        {
          "name":"support",
          "system_prompt":"Answer as a concise support agent.",
          "allowed_local_tools":["echo","current_time"]
        },
        {
          "name":"math",
          "system_prompt":"Prefer exact arithmetic over estimation.",
          "allowed_local_tools":["calculator"]
        }
      ]
    }
  }
}
```

With the server running:

```bash
env UV_CACHE_DIR=.uv-cache uv run python scripts/demo_client.py --tenant-id demo-tenant "/tool echo hello from support"
env UV_CACHE_DIR=.uv-cache uv run python scripts/demo_client.py --tenant-id demo-tenant --skill-name math "/tool echo blocked by skill"
env UV_CACHE_DIR=.uv-cache uv run python scripts/demo_client.py --tenant-id demo-tenant --skill-name missing "hello"
```

Expected results:
- Default `support` skill allows `echo`, so the reply includes a tool result.
- `math` narrows tool access, so `/tool echo ...` falls back to a plain mock reply.
- Unknown skills are rejected during thread creation with `400`.

## Example flow

```bash
curl -X POST http://127.0.0.1:8000/threads \
  -H 'Authorization: Bearer dev-token'
curl -X POST http://127.0.0.1:8000/threads/<thread_id>/messages \
  -H 'Authorization: Bearer dev-token' \
  -H 'content-type: application/json' \
  -d '{"content":"hello"}'
curl -X POST http://127.0.0.1:8000/threads/<thread_id>/run \
  -H 'Authorization: Bearer dev-token'
```

To force a tool call through the mock adapter:

```bash
curl -X POST http://127.0.0.1:8000/threads/<thread_id>/messages \
  -H 'Authorization: Bearer dev-token' \
  -H 'content-type: application/json' \
  -d '{"content":"/tool echo hello from tool"}'
curl -X POST http://127.0.0.1:8000/threads/<thread_id>/run \
  -H 'Authorization: Bearer dev-token'
```

Local tools currently include:
- `echo`
- `current_time`
- `fetch_url`
- `sleep`
- `calculator`
