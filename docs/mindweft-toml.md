# `mindweft.toml` unified config

`mindweft.toml` is Mindweft's user-facing config facade for local and desktop-style use.
It keeps common app, LLM, coding workspace, MCP, voice, and quality settings in one file
while preserving the existing environment-variable based internals for deployment and
advanced overrides.

Create a starter file:

```bash
uv run mindweft config init --profile local-coding
```

Available init profiles are `basic-chat`, `openrouter`, `local-coding`, and `voice`.
Use `--output` to write a different path and `--force` to overwrite an existing file.

Inspect the local resolved mapping:

```bash
uv run mindweft config print --resolved
```

Export a best-effort unified config from a running server:

```bash
uv run mindweft config export --output mindweft.exported.toml
```

Check common config problems:

```bash
uv run mindweft config doctor
```


Mindweft validates `mindweft.toml` against a typed schema before mapping it to environment
variables. Unknown keys and wrong value types are reported as configuration errors.

## Exporting from a running server

`mindweft config export` converts the public `/config` response from a running API into a
best-effort `mindweft.toml` facade:

```bash
uv run mindweft config export
uv run mindweft config export --output mindweft.exported.toml
uv run mindweft config export --local-coding --coding-env-file .env.coding --output mindweft.toml
uv run mindweft-coding-workspace config export --env-file .env.coding --output mindweft.toml
uv run mindweft config export --include-runtime --output mindweft.snapshot.toml
uv run mindweft --json config export --output mindweft.exported.json
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
`sops exec-file ... 'uv run mindweft-coding-workspace config export --env-file {}'` from trying
to read a one-shot decrypted file twice. If the API-only export contains local coding gateway
MCP URLs without a `coding` launch section, the command adds a TOML comment pointing you to
`--local-coding` / `mindweft-coding-workspace config export`.

## Loading and precedence

By default Mindweft looks for `.env` in the current working directory and discovers TOML
configuration in this order:

1. `MINDWEFT_CONFIG_FILE`, then legacy `MINIGENT_CONFIG_FILE`, when explicitly set;
2. `./mindweft.toml`, then legacy `./minigent.toml`, in the current working directory;
3. `$XDG_CONFIG_HOME/mindweft/mindweft.toml`, then the legacy Minigent user path, when
   `XDG_CONFIG_HOME` is absolute;
4. `~/.config/mindweft/mindweft.toml`, then `~/.config/minigent/minigent.toml`.

Canonical `MINDWEFT_*` environment variables take precedence over matching legacy
`MINIGENT_*` names. This lets a project-local config override the user-level default.
Coding-workspace commands
only use `./.env.coding` as their runner dotenv default. Set `MINDWEFT_DOTENV_FILE` to choose
a different dotenv file:

```bash
MINDWEFT_CONFIG_FILE=~/.config/mindweft/config.toml \
MINDWEFT_DOTENV_FILE=~/.config/mindweft/secrets.env \
uv run uvicorn app.main:app
```

The `mindweft` CLI also accepts `--env-file` for client-side commands:

```bash
uv run mindweft --env-file ~/.config/mindweft/client.env config doctor
```

For isolated tests or subprocesses that must ignore cwd-local and user-level default files
while still honoring explicit config paths, set `MINDWEFT_CONFIG_DISCOVERY=disabled` or call
the config loader with default discovery disabled.

Precedence is:

```text
real environment > selected .env > mindweft.toml > built-in defaults
```

This means `mindweft.toml` is safe to use as a friendly baseline while keeping secrets and
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

### Named LLM profiles

Keep multiple provider/model settings in one config by giving each one a profile name:

```toml
[llm]
default = "claude"

[llm.providers.claude]
provider = "anthropic"
model = "claude-sonnet-4-5"
api_key_env = "ANTHROPIC_API_KEY"

[llm.providers.gpt]
provider = "openai"
model = "gpt-5"
api_key_env = "OPENAI_API_KEY"

[llm.providers.local]
provider = "openai-compatible"
model = "qwen3"
base_url = "http://localhost:11434/v1"
api_key_env = "LOCAL_LLM_API_KEY"
```

The default profile is used when a client does not select one. Select a profile for a new
thread with `mindweft chat --llm gpt`, `mindweft run --llm local`, `mindweft threads create
--llm claude`, the interactive `/llm <name>` command, or the browser settings panel. The
selected profile is stored on the thread; existing threads do not change when the config
default changes. With multiple profiles, `llm.default` is required. The legacy single-provider
`[llm]` form remains supported, but it cannot be combined with `llm.providers`.

Each profile supports `provider`, `model`, `url`, `base_url`, `extra_headers`, `timeout`,
`api_key_env`, and `api_key`. Keep secrets out of TOML by using `api_key_env`; this also
allows two profiles for the same provider to use different keys.

## Init profiles

`mindweft config init` can generate a few focused starter configs:

| Profile | Purpose |
| --- | --- |
| `basic-chat` | Minimal local chat using the mock LLM provider. |
| `openrouter` | OpenRouter-backed chat with `api_key_env = "OPENROUTER_API_KEY"`. |
| `local-coding` | Coding workspace-oriented config; this is the default. |
| `voice` | Voice-oriented identity/provider facade; detailed audio tuning remains env-based. |

Examples:

```bash
uv run mindweft config init --profile basic-chat
uv run mindweft config init --profile openrouter --output openrouter.toml
uv run mindweft config init --profile voice --force
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
model = "claude-opus-5"
api_key_env = "ANTHROPIC_API_KEY"
# Optional override; defaults to https://api.anthropic.com/v1.
base_url = "https://api.anthropic.com/v1"
# Optional Anthropic prompt caching; defaults to true.
prompt_cache_enabled = true
# Optional extended thinking / reasoning for supported Claude models.
thinking_enabled = true
thinking_budget_tokens = 1024
# Claude Opus/Sonnet 4.6 and later use adaptive thinking; this controls its depth.
thinking_effort = "high"
```

`prompt_cache_enabled` maps to `ANTHROPIC_PROMPT_CACHE_ENABLED`. Prompt caching defaults to
true for Anthropic and sends top-level `cache_control = { type = "ephemeral" }`; set it to
false to omit `cache_control`.

`thinking_budget_tokens` maps to `ANTHROPIC_THINKING_BUDGET_TOKENS`; setting it enables
Anthropic thinking. You can also set `thinking_enabled = true` without a budget to use
Mindweft retains Minigent's compatibility default of 1024 tokens. For older Claude models, Mindweft sends
`thinking: { type = "enabled", budget_tokens = ... }`. Claude Opus/Sonnet 4.6 and later,
including Claude 5 models, instead receive
`thinking: { type = "adaptive", display = "summarized" }` plus
`output_config: { effort = ... }`; `thinking_effort` maps to
`ANTHROPIC_THINKING_EFFORT` and defaults to `high`. This model-aware translation preserves
existing configs while avoiding the unsupported manual-thinking shape on Claude Opus 4.8
and Claude 5 models. Claude Opus 5 enables adaptive thinking by default; enabling it here
also sends the configured effort explicitly.

### Local coding workspace

```toml
profile = "local-coding"

[llm]
provider = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "OPENROUTER_API_KEY"

[image_input]
# Enable CLI and browser image attachments when using a vision-capable model/provider.
enabled = true
# max_bytes = 5242880
# max_images = 8
# max_total_bytes = 20971520
# allowed_mime_types = ["image/png", "image/jpeg", "image/webp", "image/gif"]

[attachments]
db_path = ".data/minigent-attachments.db"
# max_per_thread = 100
# max_bytes_per_thread = 268435456

[coding]
enabled = true
workspaces = ["/Users/you/code"]
default_workspace_scope = "my-app"
shell_enabled = true
shell_allowed_command_prefixes = ["uv ", "pytest ", "git "]
mcp_gateway_enabled = true
mcp_gateway_port = 8765
mcp_gateway_path_prefix = "/mcp"
bridge_deny_globs = ["**/.env*", "**/.git/**"]
bridge_allow_globs = ["**/.env*.template"]

[coding.workspace_scopes.my-app]
roots = ["/Users/you/code/my-app"]
description = "Primary app repository"
```

### Coding MCP server specs

Use `[[coding.mcp_server_specs]]` to keep workspace MCP launch/connect definitions directly in
`mindweft.toml`:

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

`mindweft.toml` is schema-checked. Unknown top-level keys, unknown section keys, and wrong
basic value types fail validation before Mindweft maps the file to environment variables.
Use the tables below as the supported key reference.

### Top-level

| Key | Maps to | Notes |
| --- | --- | --- |
| `profile` | no direct env var | Used by tooling and docs to describe the intended local setup. |
| `image_input` | `MINDWEFT_IMAGE_INPUT_*` | Enables and limits image attachments for multimodal providers. |
| `agent_skills` | projects into `MINDWEFT_TENANT_EXECUTION_CONFIGS` | Imports local Agent Skill `SKILL.md` metadata into each tenant skill catalog. |
| `peer_agents` | `MINDWEFT_PEER_AGENTS` | Serialized as compact JSON. |
| `tenant_execution_configs` | `MINDWEFT_TENANT_EXECUTION_CONFIGS` | Serialized as compact JSON. |

### `[app]`

| Key | Maps to |
| --- | --- |
| `host` | `MINDWEFT_HOST` |
| `port` | `MINDWEFT_PORT` |
| `base_url` | `MINDWEFT_BASE_URL` |
| `thread_db_path` | `MINDWEFT_THREAD_DB_PATH` |
| `max_iterations` | `MINDWEFT_MAX_ITERATIONS` |
| `tool_timeout_seconds` | `MINDWEFT_TOOL_TIMEOUT_SECONDS` |
| `context_compaction_enabled` | `MINDWEFT_CONTEXT_COMPACTION_ENABLED` |

### `[auth]`

| Key | Maps to |
| --- | --- |
| `mode` | `MINDWEFT_AUTH_MODE` |
| `tokens` | `MINDWEFT_AUTH_TOKENS` |
| `jwt_issuer` | `MINDWEFT_JWT_ISSUER` |
| `jwt_audience` | `MINDWEFT_JWT_AUDIENCE` |
| `jwt_shared_secret` | `MINDWEFT_JWT_SHARED_SECRET` |
| `jwt_jwks_url` | `MINDWEFT_JWT_JWKS_URL` |
| `jwt_jwks_cache_seconds` | `MINDWEFT_JWT_JWKS_CACHE_SECONDS` |
| `jwt_algorithms` | `MINDWEFT_JWT_ALGORITHMS` |
| `jwt_user_claim` | `MINDWEFT_JWT_USER_CLAIM` |
| `jwt_tenant_claim` | `MINDWEFT_JWT_TENANT_CLAIM` |
| `jwt_admin_claim` | `MINDWEFT_JWT_ADMIN_CLAIM` |

### `[llm]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `default` | `MINDWEFT_LLM_DEFAULT_PROFILE` | Default named profile when using `llm.providers`. |
| `providers` | `MINDWEFT_LLM_PROFILES` | Named provider tables; selection is bound to each thread. |
| `provider` | `MINDWEFT_LLM_PROVIDER` | Examples: `mock`, `openai`, `openrouter`, `google`, `gemini`, `generic-oauth`. |
| `model` | `MINDWEFT_LLM_MODEL` plus provider model env | Also maps to `OPENAI_MODEL`, `OPENROUTER_MODEL`, or Gemini/Google model env when applicable. |
| `url` | `MINDWEFT_LLM_URL` | Useful for `generic-oauth` / compatible endpoints. |
| `base_url` | provider base URL env or `MINDWEFT_LLM_URL` | Maps to `OPENAI_BASE_URL`, `OPENROUTER_BASE_URL`, or `GOOGLE_BASE_URL` for known providers. |
| `extra_headers` | `MINDWEFT_LLM_EXTRA_HEADERS` | Serialized as compact JSON. |
| `input_modalities` | `MINDWEFT_LLM_INPUT_MODALITIES` | Optional declared inputs: `text`, `image`, `audio`, `video`, or `document`. Image messages fail early when the selected profile omits `image`. |
| `account_id_header` | `MINDWEFT_LLM_ACCOUNT_ID_HEADER` | Generic OAuth/account routing. |
| `api_key_env` | provider API key env | Copies the named env value into the provider-specific key env when present. |
| `api_key` | provider API key env | Convenience only; prefer `api_key_env`. |

When `tenant_execution_configs` is present, this top-level `[llm]` is also projected
internally as the default tenant LLM for any tenant that does not define its own
`tenant_execution_configs.<tenant>.llm` block. This keeps exported unified configs
restartable while preserving explicit tenant-level LLM overrides. `input_modalities` is
optional for backward compatibility; when omitted, Mindweft leaves provider capability
validation to the provider.

Provider key targets:

| Provider | Target env |
| --- | --- |
| `openai` | `OPENAI_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `google`, `gemini`, `google-generative-ai` | `GEMINI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |

### `[image_input]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `enabled` | `MINDWEFT_IMAGE_INPUT_ENABLED` | Enables image parts from the CLI and browser client; requires a vision-capable model/provider. |
| `max_bytes` | `MINDWEFT_IMAGE_INPUT_MAX_BYTES` | Maximum base64-decoded size of each inline image in bytes. |
| `max_images` | `MINDWEFT_IMAGE_INPUT_MAX_IMAGES` | Maximum number of image parts in one message; defaults to 8. |
| `max_total_bytes` | `MINDWEFT_IMAGE_INPUT_MAX_TOTAL_BYTES` | Maximum combined decoded size of inline images in one message; defaults to 20 MiB. |
| `max_pixels` | `MINDWEFT_IMAGE_INPUT_MAX_PIXELS` | Maximum width × height for PNG, JPEG, GIF, and WebP images; defaults to 64 million pixels. |
| `max_dimension` | `MINDWEFT_IMAGE_INPUT_MAX_DIMENSION` | Maximum width or height for PNG, JPEG, GIF, and WebP images; defaults to 16,384 pixels. |
| `allowed_mime_types` | `MINDWEFT_IMAGE_INPUT_ALLOWED_MIME_TYPES` | String or list of image MIME types; lists are converted to comma-separated env strings. |

Image parts must use exactly one source. Inline `data` must be valid base64 and match known
configured image signatures; uploaded and inline PNG, JPEG, GIF, and WebP images must also contain
readable dimensions within the configured pixel and edge limits. These header checks reject
pathological dimensions before bytes are persisted or sent to a model provider. Explicitly
configured formats without a built-in parser and remote HTTP(S) URLs remain subject to provider-side
validation. The browser streams raw image bodies to the binary attachment endpoint first and stores
`attachment_id` references in message history. The original JSON/base64 upload endpoint remains
available for compatibility. The browser preview lets users choose `auto`, `low`, or `high` detail;
providers that support image detail receive that value, while other providers use their
native/default behavior.
The dedicated camera action is shown only on coarse-pointer touch devices; desktop browsers retain
the regular image picker because they commonly ignore the HTML camera-capture hint.

### `[attachments]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `db_path` | `MINDWEFT_ATTACHMENT_DB_PATH` | Optional SQLite store for attachment bytes. Without it, attachments are process-local and disappear on restart. Use a shared path for multiple replicas. |
| `max_per_thread` | `MINDWEFT_ATTACHMENT_MAX_PER_THREAD` | Maximum stored attachment records per thread; defaults to 100. |
| `max_bytes_per_thread` | `MINDWEFT_ATTACHMENT_MAX_BYTES_PER_THREAD` | Maximum aggregate attachment bytes per thread; defaults to 256 MiB. |
| `max_per_tenant` | `MINDWEFT_ATTACHMENT_MAX_PER_TENANT` | Maximum stored attachment records across all of one tenant's threads; defaults to 1,000. |
| `max_bytes_per_tenant` | `MINDWEFT_ATTACHMENT_MAX_BYTES_PER_TENANT` | Maximum aggregate attachment bytes across one tenant; defaults to 1 GiB. |
| `pending_ttl_seconds` | `MINDWEFT_ATTACHMENT_PENDING_TTL_SECONDS` | Time before an uploaded attachment that was never referenced by a message is eligible for deletion; defaults to 24 hours. |
| `cleanup_interval_seconds` | `MINDWEFT_ATTACHMENT_CLEANUP_INTERVAL_SECONDS` | Interval between background pending-upload cleanup passes; defaults to 1 hour. |

Attachment records are scoped by tenant and thread. Provider requests resolve references to
image bytes only in transient model-facing message copies; stored messages retain references.
Deleting a thread deletes its attachment records. Unreferenced uploads can be deleted explicitly;
clients clean them up automatically if a later upload or message creation fails. New uploads are
pending until a message claims them. Pending uploads that outlive the configured TTL are removed by
a periodic multi-replica-safe cleanup pass and immediately before new uploads, releasing their
thread and tenant quota. Message creation marks references before storing history and rolls those
marks back if persistence fails, prioritizing preservation of referenced media. Rows created before
lifecycle tracking was introduced remain cleanup-exempt for backward compatibility. Per-thread and
per-tenant quotas are checked atomically when an attachment is inserted, including across
SQLite-backed replicas, so concurrent uploads cannot race past the configured storage limits.
Scheduled cleanup writes a structured completion log with its trigger, deleted record count,
deleted bytes, and duration; quota rejections log the rejected limit and incoming byte count.
Admins can inspect tenant-scoped aggregate usage through
`GET /admin/tenants/{tenant_id}/attachments/statistics`. The response separates pending,
referenced, and lifecycle-exempt records, reports the oldest pending age and configured tenant
limits, and never returns attachment IDs, creator identities, filenames, or contents.

Attachment encryption settings are intentionally environment-only so key material is not written to
`mindweft.toml`:

| Variable | Purpose |
| --- | --- |
| `MINDWEFT_ATTACHMENT_ENCRYPTION_KEY` | URL-safe base64-encoded 32-byte active AES-256-GCM key. |
| `MINDWEFT_ATTACHMENT_ENCRYPTION_KEYS` | JSON object mapping key versions to URL-safe base64 keys, used during rotation. |
| `MINDWEFT_ATTACHMENT_KEY_VERSION` | Positive active key version; defaults to 1. |
| `MINDWEFT_ATTACHMENT_REENCRYPT_ON_STARTUP` | Re-encrypt plaintext and older-version rows with the active key during startup. |

When a key is configured, new attachment bytes are encrypted with AES-256-GCM and their tenant,
thread, attachment ID, MIME type, size, creator, timestamp, and key version are authenticated as
associated data. Startup fails closed when an encrypted row requires a missing key or authentication
fails. Existing plaintext rows remain readable for migration; enable re-encryption on startup after
provisioning the keyring to encrypt them. Protect encryption keys separately from the database and
retain old key versions until rotation has completed on every replica.

### `[rate_limits]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `db_path` | `MINDWEFT_RATE_LIMIT_DB_PATH` | Optional SQLite token-bucket store. Use one shared path for multi-replica enforcement. |
| `upload_tenant_capacity` | `MINDWEFT_UPLOAD_RATE_LIMIT_TENANT_CAPACITY` | Upload burst shared by a tenant; `0` disables this bucket. |
| `upload_tenant_refill_per_second` | `MINDWEFT_UPLOAD_RATE_LIMIT_TENANT_REFILL_PER_SECOND` | Tenant upload tokens restored per second. |
| `upload_user_capacity` | `MINDWEFT_UPLOAD_RATE_LIMIT_USER_CAPACITY` | Upload burst per tenant/user pair; `0` disables this bucket. |
| `upload_user_refill_per_second` | `MINDWEFT_UPLOAD_RATE_LIMIT_USER_REFILL_PER_SECOND` | User upload tokens restored per second. |
| `run_tenant_capacity` | `MINDWEFT_RUN_RATE_LIMIT_TENANT_CAPACITY` | Run burst shared by a tenant; `0` disables this bucket. |
| `run_tenant_refill_per_second` | `MINDWEFT_RUN_RATE_LIMIT_TENANT_REFILL_PER_SECOND` | Tenant run tokens restored per second. |
| `run_user_capacity` | `MINDWEFT_RUN_RATE_LIMIT_USER_CAPACITY` | Run burst per tenant/user pair; `0` disables this bucket. |
| `run_user_refill_per_second` | `MINDWEFT_RUN_RATE_LIMIT_USER_REFILL_PER_SECOND` | User run tokens restored per second. |
| `concurrent_run_tenant_capacity` | `MINDWEFT_RUN_CONCURRENCY_TENANT_CAPACITY` | Maximum active leased runs across a tenant; `0` disables this scope. |
| `concurrent_run_user_capacity` | `MINDWEFT_RUN_CONCURRENCY_USER_CAPACITY` | Maximum active leased runs per tenant/user pair; `0` disables this scope. |
| `concurrent_run_lease_seconds` | `MINDWEFT_RUN_CONCURRENCY_LEASE_SECONDS` | Crash-recovery lease duration; defaults to 60 seconds. |
| `concurrent_run_heartbeat_seconds` | `MINDWEFT_RUN_CONCURRENCY_HEARTBEAT_SECONDS` | Renewal interval, which must be shorter than the lease; defaults to 20 seconds. |

Upload limits cover both attachment upload endpoints. Run limits cover standard and NDJSON-streamed
runs, and both variants consume the same run bucket. Every accepted request atomically consumes the
applicable tenant and user tokens; a rejection consumes neither. Rejections return HTTP 429 with a
bounded integer `Retry-After` header and a structured body, and logs contain category, tenant,
rejected scope, and retry delay without request contents. Limits default to disabled. When enabling
limits on more than one replica, configure a shared SQLite path rather than process-local state.
Concurrent-run leases use the same store and cover standard runs, streamed runs, and resumed private
consent actions. Leases are renewed while work is active and released on completion, failure,
cancellation, streaming disconnect, or shutdown. An expired lease is removed atomically during the
next acquisition or admin statistics read, so a crashed replica cannot reserve capacity forever.
Admins can inspect aggregate active-run and active-user counts without user IDs or lease IDs at
`GET /admin/tenants/{tenant_id}/run-concurrency`.

### `[coding]`

| Key | Maps to |
| --- | --- |
| `enabled` | `MINDWEFT_CODING_MCP_GATEWAY_ENABLED` |
| `tenant_id` | `MINDWEFT_CODING_TENANT_ID` |
| `workspace` | `MINDWEFT_CODING_WORKSPACE` |
| `workspaces` | `MINDWEFT_CODING_WORKSPACES` |
| `default_workspace_scope` | `MINDWEFT_CODING_DEFAULT_WORKSPACE_SCOPE` |
| `workspace_scope` | `MINDWEFT_CODING_WORKSPACE_SCOPE` |
| `workspace_scopes` | `MINDWEFT_CODING_WORKSPACE_SCOPES` |
| `inject_workspace_skill` | `MINDWEFT_CODING_INJECT_WORKSPACE_SKILL` |
| `shell_enabled` | `MINDWEFT_CODING_SHELL_ENABLED` |
| `shell_allowed_command_prefixes` | `MINDWEFT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES` |
| `bridge_host` | `MINDWEFT_CODING_BRIDGE_HOST` |
| `bridge_port` | `MINDWEFT_CODING_BRIDGE_PORT` |
| `bridge_allow_globs` | `MINDWEFT_CODING_BRIDGE_ALLOW_GLOBS` |
| `bridge_deny_globs` | `MINDWEFT_CODING_BRIDGE_DENY_GLOBS` |
| `mcp_gateway_enabled` | `MINDWEFT_CODING_MCP_GATEWAY_ENABLED` |
| `mcp_gateway_port` | `MINDWEFT_CODING_MCP_GATEWAY_PORT` |
| `mcp_gateway_path_prefix` | `MINDWEFT_CODING_MCP_GATEWAY_PATH_PREFIX` |
| `mcp_server_specs` | `MINDWEFT_CODING_MCP_SERVER_SPECS` |

List values such as `workspaces`, `shell_allowed_command_prefixes`, and bridge glob lists
are converted to comma-separated env strings. `mcp_server_specs` and `workspace_scopes` are
serialized as compact JSON. Workspace scopes use nested tables like
`[coding.workspace_scopes.<name>]` with `roots = [...]` and optional `description`; the coding
runner treats them as advisory prompt/runner narrowing until tool-level enforcement is added.

### `[agent_skills]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `dirs` | projects into `tenant_execution_configs.*.skills.items` | String or list of local directories containing child Agent Skill packages. Relative paths resolve from `mindweft.toml`. |
| `directories` | same as `dirs` | Alias for `dirs`. |

Each direct child directory with a `SKILL.md` is imported as a Mindweft-selectable skill with
`instruction_source = { type = "agent_skill", path = ".../SKILL.md" }`. Only frontmatter
metadata is read for the catalog; the `SKILL.md` body is loaded lazily when selected. Imported
Agent Skill names must not duplicate each other or existing configured skill names.

### `[mcp]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `broker_enabled` | `MINDWEFT_MCP_BROKER_ENABLED` | Enables broker path for compatible peer backends. |
| `broker_url` | `MINDWEFT_MCP_BROKER_URL` | Broker URL. |
| `servers` | `MINDWEFT_MCP_SERVERS` | Serialized as compact JSON. |

`config doctor` validates that `servers` is a list and each entry has a unique `name` and a
`url`.

### `[voice]`

The unified facade intentionally exposes only common voice identity/provider settings.
Detailed audio and VAD tuning remain available through `MINDWEFT_VOICE_*` env vars.

| Key | Maps to |
| --- | --- |
| `api_token` | `MINDWEFT_VOICE_API_TOKEN` |
| `tenant_id` | `MINDWEFT_VOICE_TENANT_ID` |
| `user_id` | `MINDWEFT_VOICE_USER_ID` |
| `thread_id` | `MINDWEFT_VOICE_THREAD_ID` |
| `skill` | `MINDWEFT_VOICE_SKILL` |
| `location` | `MINDWEFT_VOICE_LOCATION` |
| `wake_phrase` | `MINDWEFT_VOICE_WAKE_PHRASE` |
| `wakeword_provider` | `MINDWEFT_VOICE_WAKEWORD_PROVIDER` |
| `stt_provider` | `MINDWEFT_VOICE_STT_PROVIDER` |
| `tts_provider` | `MINDWEFT_VOICE_TTS_PROVIDER` |

### `[quality]`

| Key | Maps to |
| --- | --- |
| `enabled` | `MINDWEFT_REMOTE_QUALITY_ENABLED` |
| `provider` | `MINDWEFT_REMOTE_QUALITY_PROVIDER` |
| `model` | `MINDWEFT_REMOTE_QUALITY_MODEL` |
| `base_url` | `MINDWEFT_REMOTE_QUALITY_BASE_URL` |
| `api_key` | `MINDWEFT_REMOTE_QUALITY_API_KEY` |
| `mode` | `MINDWEFT_REMOTE_QUALITY_MODE` |
| `timeout` | `MINDWEFT_REMOTE_QUALITY_TIMEOUT` |
| `max_payload_chars` | `MINDWEFT_REMOTE_QUALITY_MAX_PAYLOAD_CHARS` |

### `[logging]`

| Key | Maps to |
| --- | --- |
| `level` | `MINDWEFT_LOG_LEVEL` |
| `format` | `MINDWEFT_LOG_FORMAT` |

## Troubleshooting

- `mindweft config print --resolved` shows the local mapping after applying `mindweft.toml`,
  `.env`, and real environment variables. Secret-looking keys are masked.
- `mindweft config doctor` validates local config first. If a blocking local problem is
  found, it does not attempt to contact the API. Schema validation catches unknown keys and
  wrong value types before env mapping.
- If a value looks wrong, check precedence. A real environment variable will override both
  `.env` and `mindweft.toml`.
