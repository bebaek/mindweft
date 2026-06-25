# `minigent.toml` unified config

`minigent.toml` is Minigent's user-facing config facade for local and desktop-style use.
It keeps common app, LLM, coding workspace, MCP, voice, and quality settings in one file
while preserving the existing environment-variable based internals for deployment and
advanced overrides.

Create a starter file:

```bash
uv run minigent config init --profile local-coding
```

Available init profiles are `basic-chat`, `openrouter`, `local-coding`, and `voice`.
Use `--output` to write a different path and `--force` to overwrite an existing file.

Inspect the local resolved mapping:

```bash
uv run minigent config print --resolved
```

Export a best-effort unified config from a running server:

```bash
uv run minigent config export --output minigent.exported.toml
```

Check common config problems:

```bash
uv run minigent config doctor
```


Minigent validates `minigent.toml` against a typed schema before mapping it to environment
variables. Unknown keys and wrong value types are reported as configuration errors.

## Exporting from a running server

`minigent config export` converts the public `/config` response from a running API into a
best-effort `minigent.toml` facade:

```bash
uv run minigent config export
uv run minigent config export --output minigent.exported.toml
uv run minigent config export --local-coding --coding-env-file .env.coding --output minigent.toml
uv run minigent-coding-workspace config export --env-file .env.coding --output minigent.toml
uv run minigent config export --include-runtime --output minigent.snapshot.toml
uv run minigent --json config export --output minigent.exported.json
```

The export cannot recover the original source files or secret values. When the server supports
`/config?export=true`, it also includes richer API-owned details such as generic OAuth settings,
tenant execution configs with skill prompts, capability profiles, and runtime MCP server
registration config. Runtime status/tools are informational and are included only with
`--include-runtime`. Provider API keys are emitted as environment references such as
`api_key_env = "OPENROUTER_API_KEY"`, and secret-looking MCP headers/env values are masked.

Use `--local-coding` when exporting a local coding workspace stack. The CLI then resolves the
local coding runner config from `--coding-env-file`, `--env-file`, or `.env.coding`, converts
legacy `MINIGENT_CODING_MCP_SERVERS_FILE` contents into inline `[[coding.mcp_server_specs]]`,
and merges those runner-owned launch settings with the API-owned server export. The command
reuses a dotenv file already loaded by `--env-file`, which keeps wrappers such as
`sops exec-file ... 'uv run minigent-coding-workspace config export --env-file {}'` from trying
to read a one-shot decrypted file twice. If the API-only export contains local coding gateway
MCP URLs without a `coding` launch section, the command adds a TOML comment pointing you to
`--local-coding` / `minigent-coding-workspace config export`.

## Loading and precedence

By default Minigent looks for `minigent.toml` and `.env` in the current working directory.
The unified TOML config default is always `./minigent.toml`; coding-workspace commands only
use `./.env.coding` as their runner dotenv default. Set
`MINIGENT_CONFIG_FILE` to choose a different TOML file, and `MINIGENT_DOTENV_FILE` to choose a
different dotenv file:

```bash
MINIGENT_CONFIG_FILE=~/.config/minigent/config.toml \
MINIGENT_DOTENV_FILE=~/.config/minigent/secrets.env \
uv run uvicorn app.main:app
```

The `minigent` CLI also accepts `--env-file` for client-side commands:

```bash
uv run minigent --env-file ~/.config/minigent/client.env config doctor
```

For isolated tests or subprocesses that must ignore cwd-local default files while still
honoring explicit config paths, set `MINIGENT_CONFIG_DISCOVERY=disabled` or call the config
loader with default discovery disabled.

Precedence is:

```text
real environment > selected .env > minigent.toml > built-in defaults
```

This means `minigent.toml` is safe to use as a friendly baseline while keeping secrets and
deployment-specific overrides in the environment or selected `.env` file.

## Secrets

Prefer references such as `api_key_env` over raw secret values:

```toml
[llm]
provider = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "OPENROUTER_API_KEY"
```

Then provide the secret out of band:

```bash
export OPENROUTER_API_KEY=...
```

Raw `api_key` entries are supported for convenience for provider-backed LLMs, but avoid
committing them.

## Init profiles

`minigent config init` can generate a few focused starter configs:

| Profile | Purpose |
| --- | --- |
| `basic-chat` | Minimal local chat using the mock LLM provider. |
| `openrouter` | OpenRouter-backed chat with `api_key_env = "OPENROUTER_API_KEY"`. |
| `local-coding` | Coding workspace-oriented config; this is the default. |
| `voice` | Voice-oriented identity/provider facade; detailed audio tuning remains env-based. |

Examples:

```bash
uv run minigent config init --profile basic-chat
uv run minigent config init --profile openrouter --output openrouter.toml
uv run minigent config init --profile voice --force
```

## Examples

### Basic local mock chat

```toml
profile = "basic-chat"

[auth]
mode = "development"

[llm]
provider = "mock"

[app]
thread_db_path = ".data/minigent-threads.db"
```

### OpenRouter

```toml
[llm]
provider = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "OPENROUTER_API_KEY"
```

### Anthropic

```toml
[llm]
provider = "anthropic"
model = "claude-haiku-4-5"
api_key_env = "ANTHROPIC_API_KEY"
# Optional override; defaults to https://api.anthropic.com/v1.
base_url = "https://api.anthropic.com/v1"
```

### Local coding workspace

```toml
profile = "local-coding"

[llm]
provider = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "OPENROUTER_API_KEY"

[coding]
enabled = true
workspaces = ["/Users/you/code"]
shell_enabled = true
shell_allowed_command_prefixes = ["uv ", "pytest ", "git "]
mcp_gateway_enabled = true
mcp_gateway_port = 8765
mcp_gateway_path_prefix = "/mcp"
bridge_deny_globs = ["**/.env*", "**/.git/**"]
bridge_allow_globs = ["**/.env*.template"]
```

### Coding MCP server specs

Use `[[coding.mcp_server_specs]]` to keep workspace MCP launch/connect definitions directly in
`minigent.toml`:

```toml
[[coding.mcp_server_specs]]
name = "web-fetch"
transport = "stdio"
command = ["uvx", "mcp-server-fetch"]
profiles = ["inspect"]
allowed_tools = ["fetch"]

[[coding.mcp_server_specs]]
name = "web-search"
transport = "http"
managed = true
command = ["npx", "-y", "@brave/brave-search-mcp-server", "--transport", "http", "--host", "127.0.0.1", "--port", "8766"]
url = "http://127.0.0.1:8766/mcp"
health_url = "http://127.0.0.1:8766/ping"
env = { BRAVE_API_KEY = "${BRAVE_API_KEY}" }
profiles = ["inspect"]
allowed_tools = ["brave_web_search", "brave_news_search", "brave_llm_context"]
```

### HTTP MCP servers

```toml
[mcp]
servers = [
  { name = "filesystem", url = "http://127.0.0.1:8765/mcp", headers = {} },
  { name = "tools", url = "https://example.com/mcp", headers = { Authorization = "Bearer token" } },
]
```

## Supported keys

`minigent.toml` is schema-checked. Unknown top-level keys, unknown section keys, and wrong
basic value types fail validation before Minigent maps the file to environment variables.
Use the tables below as the supported key reference.

### Top-level

| Key | Maps to | Notes |
| --- | --- | --- |
| `profile` | no direct env var | Used by tooling and docs to describe the intended local setup. |
| `peer_agents` | `MINIGENT_PEER_AGENTS` | Serialized as compact JSON. |
| `tenant_execution_configs` | `MINIGENT_TENANT_EXECUTION_CONFIGS` | Serialized as compact JSON. |

### `[app]`

| Key | Maps to |
| --- | --- |
| `host` | `MINIGENT_HOST` |
| `port` | `MINIGENT_PORT` |
| `base_url` | `MINIGENT_BASE_URL` |
| `thread_db_path` | `MINIGENT_THREAD_DB_PATH` |
| `max_iterations` | `MINIGENT_MAX_ITERATIONS` |
| `tool_timeout_seconds` | `MINIGENT_TOOL_TIMEOUT_SECONDS` |
| `context_compaction_enabled` | `MINIGENT_CONTEXT_COMPACTION_ENABLED` |

### `[auth]`

| Key | Maps to |
| --- | --- |
| `mode` | `MINIGENT_AUTH_MODE` |
| `tokens` | `MINIGENT_AUTH_TOKENS` |
| `jwt_issuer` | `MINIGENT_JWT_ISSUER` |
| `jwt_audience` | `MINIGENT_JWT_AUDIENCE` |
| `jwt_shared_secret` | `MINIGENT_JWT_SHARED_SECRET` |
| `jwt_jwks_url` | `MINIGENT_JWT_JWKS_URL` |
| `jwt_jwks_cache_seconds` | `MINIGENT_JWT_JWKS_CACHE_SECONDS` |
| `jwt_algorithms` | `MINIGENT_JWT_ALGORITHMS` |
| `jwt_user_claim` | `MINIGENT_JWT_USER_CLAIM` |
| `jwt_tenant_claim` | `MINIGENT_JWT_TENANT_CLAIM` |
| `jwt_admin_claim` | `MINIGENT_JWT_ADMIN_CLAIM` |

### `[llm]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `provider` | `MINIGENT_LLM_PROVIDER` | Examples: `mock`, `openai`, `openrouter`, `google`, `gemini`, `generic-oauth`. |
| `model` | `MINIGENT_LLM_MODEL` plus provider model env | Also maps to `OPENAI_MODEL`, `OPENROUTER_MODEL`, or Gemini/Google model env when applicable. |
| `url` | `MINIGENT_LLM_URL` | Useful for `generic-oauth` / compatible endpoints. |
| `base_url` | provider base URL env or `MINIGENT_LLM_URL` | Maps to `OPENAI_BASE_URL`, `OPENROUTER_BASE_URL`, or `GOOGLE_BASE_URL` for known providers. |
| `extra_headers` | `MINIGENT_LLM_EXTRA_HEADERS` | Serialized as compact JSON. |
| `account_id_header` | `MINIGENT_LLM_ACCOUNT_ID_HEADER` | Generic OAuth/account routing. |
| `api_key_env` | provider API key env | Copies the named env value into the provider-specific key env when present. |
| `api_key` | provider API key env | Convenience only; prefer `api_key_env`. |

Provider key targets:

| Provider | Target env |
| --- | --- |
| `openai` | `OPENAI_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `google`, `gemini`, `google-generative-ai` | `GEMINI_API_KEY` |

### `[coding]`

| Key | Maps to |
| --- | --- |
| `enabled` | `MINIGENT_CODING_MCP_GATEWAY_ENABLED` |
| `tenant_id` | `MINIGENT_CODING_TENANT_ID` |
| `workspace` | `MINIGENT_CODING_WORKSPACE` |
| `workspaces` | `MINIGENT_CODING_WORKSPACES` |
| `inject_workspace_skill` | `MINIGENT_CODING_INJECT_WORKSPACE_SKILL` |
| `shell_enabled` | `MINIGENT_CODING_SHELL_ENABLED` |
| `shell_allowed_command_prefixes` | `MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES` |
| `bridge_host` | `MINIGENT_CODING_BRIDGE_HOST` |
| `bridge_port` | `MINIGENT_CODING_BRIDGE_PORT` |
| `bridge_allow_globs` | `MINIGENT_CODING_BRIDGE_ALLOW_GLOBS` |
| `bridge_deny_globs` | `MINIGENT_CODING_BRIDGE_DENY_GLOBS` |
| `mcp_gateway_enabled` | `MINIGENT_CODING_MCP_GATEWAY_ENABLED` |
| `mcp_gateway_port` | `MINIGENT_CODING_MCP_GATEWAY_PORT` |
| `mcp_gateway_path_prefix` | `MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX` |
| `mcp_server_specs` | `MINIGENT_CODING_MCP_SERVER_SPECS` |

List values such as `workspaces`, `shell_allowed_command_prefixes`, and bridge glob lists
are converted to comma-separated env strings. `mcp_server_specs` is serialized as compact JSON.

### `[mcp]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `broker_enabled` | `MINIGENT_MCP_BROKER_ENABLED` | Enables broker path for compatible peer backends. |
| `broker_url` | `MINIGENT_MCP_BROKER_URL` | Broker URL. |
| `servers` | `MINIGENT_MCP_SERVERS` | Serialized as compact JSON. |

`config doctor` validates that `servers` is a list and each entry has a unique `name` and a
`url`.

### `[voice]`

The unified facade intentionally exposes only common voice identity/provider settings.
Detailed audio and VAD tuning remain available through `MINIGENT_VOICE_*` env vars.

| Key | Maps to |
| --- | --- |
| `api_token` | `MINIGENT_VOICE_API_TOKEN` |
| `tenant_id` | `MINIGENT_VOICE_TENANT_ID` |
| `user_id` | `MINIGENT_VOICE_USER_ID` |
| `thread_id` | `MINIGENT_VOICE_THREAD_ID` |
| `skill` | `MINIGENT_VOICE_SKILL` |
| `location` | `MINIGENT_VOICE_LOCATION` |
| `wake_phrase` | `MINIGENT_VOICE_WAKE_PHRASE` |
| `wakeword_provider` | `MINIGENT_VOICE_WAKEWORD_PROVIDER` |
| `stt_provider` | `MINIGENT_VOICE_STT_PROVIDER` |
| `tts_provider` | `MINIGENT_VOICE_TTS_PROVIDER` |

### `[quality]`

| Key | Maps to |
| --- | --- |
| `enabled` | `MINIGENT_REMOTE_QUALITY_ENABLED` |
| `provider` | `MINIGENT_REMOTE_QUALITY_PROVIDER` |
| `model` | `MINIGENT_REMOTE_QUALITY_MODEL` |
| `base_url` | `MINIGENT_REMOTE_QUALITY_BASE_URL` |
| `api_key` | `MINIGENT_REMOTE_QUALITY_API_KEY` |
| `mode` | `MINIGENT_REMOTE_QUALITY_MODE` |
| `timeout` | `MINIGENT_REMOTE_QUALITY_TIMEOUT` |
| `max_payload_chars` | `MINIGENT_REMOTE_QUALITY_MAX_PAYLOAD_CHARS` |

### `[logging]`

| Key | Maps to |
| --- | --- |
| `level` | `MINIGENT_LOG_LEVEL` |
| `format` | `MINIGENT_LOG_FORMAT` |

## Troubleshooting

- `minigent config print --resolved` shows the local mapping after applying `minigent.toml`,
  `.env`, and real environment variables. Secret-looking keys are masked.
- `minigent config doctor` validates local config first. If a blocking local problem is
  found, it does not attempt to contact the API. Schema validation catches unknown keys and
  wrong value types before env mapping.
- If a value looks wrong, check precedence. A real environment variable will override both
  `.env` and `minigent.toml`.
