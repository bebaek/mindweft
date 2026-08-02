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

By default Minigent looks for `.env` in the current working directory and discovers TOML
configuration in this order:

1. `MINIGENT_CONFIG_FILE`, when explicitly set;
2. `./minigent.toml` in the current working directory;
3. `$XDG_CONFIG_HOME/minigent/minigent.toml`, when `XDG_CONFIG_HOME` is an absolute path;
4. `~/.config/minigent/minigent.toml`.

This lets a project-local config override the user-level default. Coding-workspace commands
only use `./.env.coding` as their runner dotenv default. Set `MINIGENT_DOTENV_FILE` to choose
a different dotenv file:

```bash
MINIGENT_CONFIG_FILE=~/.config/minigent/config.toml \
MINIGENT_DOTENV_FILE=~/.config/minigent/secrets.env \
uv run uvicorn app.main:app
```

The `minigent` CLI also accepts `--env-file` for client-side commands:

```bash
uv run minigent --env-file ~/.config/minigent/client.env config doctor
```

For isolated tests or subprocesses that must ignore cwd-local and user-level default files
while still honoring explicit config paths, set `MINIGENT_CONFIG_DISCOVERY=disabled` or call
the config loader with default discovery disabled.

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
thread with `minigent chat --llm gpt`, `minigent run --llm local`, `minigent threads create
--llm claude`, the interactive `/llm <name>` command, or the browser settings panel. The
selected profile is stored on the thread; existing threads do not change when the config
default changes. With multiple profiles, `llm.default` is required. The legacy single-provider
`[llm]` form remains supported, but it cannot be combined with `llm.providers`.

Each profile supports `provider`, `model`, `url`, `base_url`, `extra_headers`, `timeout`,
`api_key_env`, and `api_key`. Keep secrets out of TOML by using `api_key_env`; this also
allows two profiles for the same provider to use different keys.

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
Minigent's compatibility default of 1024 tokens. For older Claude models, Minigent sends
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
| `image_input` | `MINIGENT_IMAGE_INPUT_*` | Enables and limits image attachments for multimodal providers. |
| `agent_skills` | projects into `MINIGENT_TENANT_EXECUTION_CONFIGS` | Imports local Agent Skill `SKILL.md` metadata into each tenant skill catalog. |
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
| `default` | `MINIGENT_LLM_DEFAULT_PROFILE` | Default named profile when using `llm.providers`. |
| `providers` | `MINIGENT_LLM_PROFILES` | Named provider tables; selection is bound to each thread. |
| `provider` | `MINIGENT_LLM_PROVIDER` | Examples: `mock`, `openai`, `openrouter`, `google`, `gemini`, `generic-oauth`. |
| `model` | `MINIGENT_LLM_MODEL` plus provider model env | Also maps to `OPENAI_MODEL`, `OPENROUTER_MODEL`, or Gemini/Google model env when applicable. |
| `url` | `MINIGENT_LLM_URL` | Useful for `generic-oauth` / compatible endpoints. |
| `base_url` | provider base URL env or `MINIGENT_LLM_URL` | Maps to `OPENAI_BASE_URL`, `OPENROUTER_BASE_URL`, or `GOOGLE_BASE_URL` for known providers. |
| `extra_headers` | `MINIGENT_LLM_EXTRA_HEADERS` | Serialized as compact JSON. |
| `input_modalities` | `MINIGENT_LLM_INPUT_MODALITIES` | Optional declared inputs: `text`, `image`, `audio`, `video`, or `document`. Image messages fail early when the selected profile omits `image`. |
| `account_id_header` | `MINIGENT_LLM_ACCOUNT_ID_HEADER` | Generic OAuth/account routing. |
| `api_key_env` | provider API key env | Copies the named env value into the provider-specific key env when present. |
| `api_key` | provider API key env | Convenience only; prefer `api_key_env`. |

When `tenant_execution_configs` is present, this top-level `[llm]` is also projected
internally as the default tenant LLM for any tenant that does not define its own
`tenant_execution_configs.<tenant>.llm` block. This keeps exported unified configs
restartable while preserving explicit tenant-level LLM overrides. `input_modalities` is
optional for backward compatibility; when omitted, Minigent leaves provider capability
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
| `enabled` | `MINIGENT_IMAGE_INPUT_ENABLED` | Enables image parts from the CLI and browser client; requires a vision-capable model/provider. |
| `max_bytes` | `MINIGENT_IMAGE_INPUT_MAX_BYTES` | Maximum base64-decoded size of each inline image in bytes. |
| `max_images` | `MINIGENT_IMAGE_INPUT_MAX_IMAGES` | Maximum number of image parts in one message; defaults to 8. |
| `max_total_bytes` | `MINIGENT_IMAGE_INPUT_MAX_TOTAL_BYTES` | Maximum combined decoded size of inline images in one message; defaults to 20 MiB. |
| `allowed_mime_types` | `MINIGENT_IMAGE_INPUT_ALLOWED_MIME_TYPES` | String or list of image MIME types; lists are converted to comma-separated env strings. |

Image parts must use exactly one source. Inline `data` must be valid base64 and match known
configured image signatures; remote URLs must be absolute HTTP(S) URLs. The browser streams raw
image bodies to the binary attachment endpoint first and stores `attachment_id` references in
message history. The original JSON/base64 upload endpoint remains available for compatibility.
The browser preview lets users choose `auto`, `low`, or `high` detail per image; providers that
support image detail receive that value, while other providers use their native/default behavior.

### `[attachments]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `db_path` | `MINIGENT_ATTACHMENT_DB_PATH` | Optional SQLite store for attachment bytes. Without it, attachments are process-local and disappear on restart. Use a shared path for multiple replicas. |
| `max_per_thread` | `MINIGENT_ATTACHMENT_MAX_PER_THREAD` | Maximum stored attachment records per thread; defaults to 100. |
| `max_bytes_per_thread` | `MINIGENT_ATTACHMENT_MAX_BYTES_PER_THREAD` | Maximum aggregate attachment bytes per thread; defaults to 256 MiB. |

Attachment records are scoped by tenant and thread. Provider requests resolve references to
image bytes only in transient model-facing message copies; stored messages retain references.
Deleting a thread deletes its attachment records. Unreferenced uploads can be deleted explicitly;
clients clean them up automatically if a later upload or message creation fails. Quotas are checked
atomically when the attachment is inserted, including across SQLite-backed replicas. Attachment
SQLite bytes are not encrypted by Minigent, so protect the database volume and backups according
to the sensitivity of uploaded media.

### `[coding]`

| Key | Maps to |
| --- | --- |
| `enabled` | `MINIGENT_CODING_MCP_GATEWAY_ENABLED` |
| `tenant_id` | `MINIGENT_CODING_TENANT_ID` |
| `workspace` | `MINIGENT_CODING_WORKSPACE` |
| `workspaces` | `MINIGENT_CODING_WORKSPACES` |
| `default_workspace_scope` | `MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE` |
| `workspace_scope` | `MINIGENT_CODING_WORKSPACE_SCOPE` |
| `workspace_scopes` | `MINIGENT_CODING_WORKSPACE_SCOPES` |
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
are converted to comma-separated env strings. `mcp_server_specs` and `workspace_scopes` are
serialized as compact JSON. Workspace scopes use nested tables like
`[coding.workspace_scopes.<name>]` with `roots = [...]` and optional `description`; the coding
runner treats them as advisory prompt/runner narrowing until tool-level enforcement is added.

### `[agent_skills]`

| Key | Maps to | Notes |
| --- | --- | --- |
| `dirs` | projects into `tenant_execution_configs.*.skills.items` | String or list of local directories containing child Agent Skill packages. Relative paths resolve from `minigent.toml`. |
| `directories` | same as `dirs` | Alias for `dirs`. |

Each direct child directory with a `SKILL.md` is imported as a Minigent-selectable skill with
`instruction_source = { type = "agent_skill", path = ".../SKILL.md" }`. Only frontmatter
metadata is read for the catalog; the `SKILL.md` body is loaded lazily when selected. Imported
Agent Skill names must not duplicate each other or existing configured skill names.

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
