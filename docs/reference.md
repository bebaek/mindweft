# Mindweft

Minimal AI agent runtime POC from `DESIGN.md`.

## What it includes

- FastAPI service
- In-memory thread/message store by default, with optional SQLite persistence
- Optional thread context compaction with rolling summary + token-budgeted recent-message tail
  that can drop summarized raw turns from the in-memory transcript to keep memory bounded
- Simple agent execution loop
- Pluggable tool registry
- Replaceable LLM adapter boundary
- OpenAI and OpenRouter support via one OpenAI-compatible adapter
- Optional generic OAuth login for provider-specific LLM integrations
- Optional MCP tool discovery and invocation over HTTP
- Optional local agent wrapper for Pi-first peer-agent task execution
- Deterministic mock adapter for local testing
- Optional privacy-preserving remote quality critique of sanitized local drafts

## Built-In Tools

The local tool registry includes:

- `echo`: returns supplied text
- `current_time`: returns the current UTC time
- `fetch_url`: fetches HTTP/HTTPS URLs with `GET` or `HEAD`, bounded timeout,
  redirect, and response-size controls
- `sleep`: pauses execution
- `calculator`: evaluates basic arithmetic
- `retrieve_knowledge`: opt-in tool that searches tenant-scoped MiniRAG knowledge when configured
- `peer_agent_task`: submits a task to a configured federated peer agent and optionally
  polls for completion when `MINIGENT_ENABLE_PEER_AGENT_TOOL=true`

`fetch_url` is intended for lightweight web context and endpoint checks, not as a full
`curl` replacement. It rejects non-HTTP schemes, private-network hosts, sensitive
request headers such as `authorization` and `cookie`, and responses larger than the
requested `max_bytes` limit are truncated in the returned text/body.

All tool results pass through the tool registry's best-effort redactor before they are
streamed, persisted in thread history, or included in later LLM context. The default
redactor recursively removes values under sensitive-looking keys such as `token`,
`secret`, `password`, `api_key`, `authorization`, and `private_key`, redacts sensitive
URL query parameters, and redacts PEM private-key blocks in text. This reduces accidental
secret propagation but is not a formal DLP boundary; avoid exposing credentials to tools
unless the task requires it.

Tool-result redaction can be configured at the tenant `tools` level and overridden per MCP
server with `result_redaction` / `resultRedaction`:

```json
{
  "tools": {
    "result_redaction": {
      "enabled": true,
      "mode": "best_effort",
      "sensitive_tools": ["filesystem.read_file", "shell.run_command"]
    },
    "mcp_servers": [
      {
        "name": "filesystem",
        "url": "http://127.0.0.1:9001/mcp",
        "headers": {},
        "timeout_seconds": 60,
        "result_redaction": {"mode": "full"}
      }
    ]
  }
}
```

Modes are:

- `best_effort`: recursively redact sensitive-looking keys, sensitive URL query params, and
  PEM private-key blocks.
- `full`: replace the entire result with metadata noting that it was redacted.
- `none`: leave results unchanged. Use only for trusted-local development or when another
  boundary already handles redaction.

`sensitive_tools` forces full-result redaction for matching fully-qualified tool names even
when `mode` is `best_effort`.

## Run

```bash
uv venv
source .venv/bin/activate
uv sync --dev
uv run uvicorn app.main:app --reload
```

## Browser clients

The production console foundation is served at `/console/`. Its source is in `web/`; use
`npm ci && npm run dev` there for frontend development and `npm run build` to refresh the static
assets packaged under `app/static/console/`. Run `npm run test:e2e:install` once, then
`npm run test:e2e` for desktop/mobile Chromium coverage and axe accessibility checks. The console
currently provides runtime readiness, connection setup, thread history, message composition,
streaming run activity, cancellation, context inspection, confirmed context compaction, validated
image selection, authenticated attachment upload/display, image detail controls, one-time
private-value approval/denial, pending-consent recovery, uncertain-action reconciliation, tenant
search/detail views, tenant and user provisioning, domain verification, entitlement and execution
configuration editing, operational capacity metrics, confirmed tenant lifecycle transitions,
filtered thread and retained-message inspection, previewed pruning, confirmed thread deletion, and
paginated audit review.

The dependency-free development client remains at `/web/` and provides the complete current chat
workflow.

`POST /threads/{thread_id}/run/stream` is also available for clients that want run
progress without waiting for the final JSON response. It returns newline-delimited JSON
with `Content-Type: application/x-ndjson`. The stream emits `run.started`, native runtime
progress such as `llm.request`, `tool.call`, and `tool.result`, peer-backend progress such
as `peer.task.created`/`peer.task.poll`/`peer.task.event`/`peer.task.completed`, then either
`assistant.message` and `run.completed`, or `run.error`. Peer task events are sanitized before
streaming: Mindweft forwards event type/status/tool metadata, emits only allowlisted tool
argument summaries, and strips raw tool arguments plus nested message content from peer agent
JSON events. Configure the peer tool argument summary allowlist with
`MINIGENT_PEER_TOOL_ARG_ALLOWLIST`, either as JSON such as
`{"read":["path","limit"],"grep":["pattern","path"]}` or as
`read:path,limit;grep:pattern,path`; set it to `off` to suppress argument summaries.
For local development only, set it to `all` or `*` to summarize every argument key after
redaction/truncation while still stripping raw arguments from streamed events.
The existing `POST /threads/{thread_id}/run` endpoint remains unchanged.
When the configured LLM provider returns token usage, streamed native runs emit an
`llm.response` event with normalized `usage` fields such as `prompt_tokens`,
`completion_tokens`, `total_tokens`, and, when reported by the provider,
`cache_read_tokens` / `cache_write_tokens` for prompt-cache diagnostics.

The API also serves a small static browser client at `/web` for quick manual testing from
desktop and mobile browsers. It uses the NDJSON run stream to display live run/tool/peer
progress before appending the final assistant reply. The client adjusts to mobile visual
viewport changes so the composer remains usable when the screen keyboard is open. It has
no frontend build step or extra dependencies.

## Prompt Cache Diagnostics

By default, Mindweft keeps thread history append-only and disables automatic context
compaction so provider-side prompt caches can reuse stable prefixes across turns. Use
`POST /threads/{thread_id}/compact` or the interactive CLI `/compact` command to manually
fold older raw messages into a deterministic thread summary while retaining the recent
message tail. Set `MINIGENT_CONTEXT_COMPACTION_ENABLED=true` to re-enable rolling summaries
and old-message compaction during runs for smaller prompts at the cost of resetting/changing
the cacheable prefix.

Mindweft does not cache model replies locally. Native LLM adapters do preserve and report
provider-side prompt-cache usage when the provider includes it in response metadata.
For provider debugging, set `MINIGENT_LLM_DEBUG_LOG_RESPONSES=true` to log raw LLM
response bodies through the Mindweft logger; responses may contain prompts, assistant
content, tool outputs, and usage metadata, so enable this only in trusted local/debug
environments. Logs are truncated to 20000 characters by default; override with
`MINIGENT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS`. Because normal logs redact/truncate long
fields, set `MINIGENT_LLM_DEBUG_RESPONSE_LOG_PATH=/tmp/minigent-llm-raw.jsonl` to also
write raw response bodies as JSONL records for detailed inspection. To inspect outbound
LLM request stability, set `MINIGENT_LLM_DEBUG_REQUEST_LOG_PATH=/tmp/minigent-llm-requests.jsonl`;
Mindweft writes raw/canonical SHA-256 hashes, message/tool counts, and the full request
payload for each provider call. Request logs can expose prompts and tool data, so only
enable them in trusted debug environments.

To run a repeatable cache probe against a running API and print the streamed usage/cache
counters, use:

```bash
MINIGENT_LLM_DEBUG_LOG_RESPONSES=true \
MINIGENT_LLM_DEBUG_RESPONSE_LOG_PATH=/tmp/minigent-llm-raw.jsonl \
  uv run uvicorn app.main:app --reload

uv run python scripts/investigate_prompt_cache.py --trace
```

For the workspace-coding setup that can read local files, put the debug/cache settings in
`.env.coding` and start Mindweft with the workspace runner instead:

```bash
MINIGENT_LLM_PROMPT_CACHE_KEY=thread
MINIGENT_LLM_DEBUG_LOG_RESPONSES=true
MINIGENT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS=200000
MINIGENT_LLM_DEBUG_RESPONSE_LOG_PATH=/tmp/minigent-llm-raw.jsonl
MINIGENT_LLM_DEBUG_REQUEST_LOG_PATH=/tmp/minigent-llm-requests.jsonl
```

```bash
uv run mindweft-coding-workspace --env-file .env.coding
uv run python scripts/investigate_prompt_cache.py --trace --pause 2 \
  --tenant-id demo-tenant --capability-profile inspect
```

The script creates a thread, runs several related README prompts, prints each run's
`prompt`/`completion`/`total`/`cache_read`/`cache_write` counters, and emits a trace ID
that can be used to correlate with raw `app.llm` response logs.

To bypass Mindweft entirely and inspect direct OpenRouter responses, use:

```bash
OPENROUTER_API_KEY=... \
  uv run python scripts/openrouter_raw_probe.py \
  --model openai/gpt-5.1-codex-mini \
  --output /tmp/openrouter-raw-probe.jsonl
```

The direct probe sends sequential `/chat/completions` requests with a repeated static
prefix above the documented prompt-cache threshold, requests OpenRouter usage metadata
with `usage: {"include": true}`, prints usage/cache summaries, and writes full request
and raw response JSONL records for inspection. Add `--mock-tool-count 1` to include a
stable mock function tool in each direct request.

To keep Mindweft's OpenAI-compatible adapter in the path while bypassing the full
agent/runtime/tool loop, use:

```bash
OPENROUTER_API_KEY=... \
  uv run python scripts/minigent_openrouter_adapter_probe.py \
  --model openai/gpt-5.1-codex-mini \
  --output /tmp/minigent-openrouter-adapter-probe.jsonl \
  --raw-output /tmp/minigent-openrouter-adapter-raw.jsonl
```

If the direct OpenRouter probe gets cache hits and this adapter probe also gets cache
hits, Mindweft's adapter request shape is cache-compatible and full agent runs are likely
missing cache hits because their long prefix is not stable enough. Add `--mock-tool-count 1`
to test the adapter path with a stable mock function tool included in every request.
OpenAI-compatible cache counters such as `prompt_tokens_details.cached_tokens` and
Responses-style `input_tokens_details.cached_tokens` are normalized to
`cache_read_tokens`; provider cache-creation counters are normalized to
`cache_write_tokens`. These counters are informational and depend on the selected model
and provider enabling prompt caching for stable prompt prefixes. For generic OAuth
Responses endpoints that support caller-selected cache buckets, Mindweft sends the current
thread ID as `prompt_cache_key` and also sends Codex/Pi-style `session_id`, `session-id`,
`thread-id`, and `x-client-request-id` headers. For the ChatGPT Codex Responses endpoint,
Mindweft also sends `include: ["reasoning.encrypted_content"]` and requests reasoning
summaries with `reasoning: {"effort":"medium","summary":"auto"}` by default, matching
Pi's visible-thinking request shape. Override those defaults with
`MINIGENT_LLM_REASONING_EFFORT` and `MINIGENT_LLM_REASONING_SUMMARY`; set either value to
`off`, `none`, `null`, `false`, or `0` to omit that field. If the endpoint returns only
encrypted reasoning state and no assistant message or tool call, Mindweft automatically
continues the Responses request with that reasoning state up to
`MINIGENT_RESPONSES_REASONING_ONLY_RETRIES` times (default `3`) before returning a
structured retryable `provider_reasoning_only` error. Set
`MINIGENT_LLM_PROMPT_CACHE_KEY` to a literal value to
override the cache key, or to `thread`/`auto` to explicitly use thread-ID mode.
For OpenRouter,
Mindweft requests usage metadata with `usage: {"include": true}` so compatible models
can report usage and cache counters.

## Google Gemini LLM Provider

Mindweft can call Gemini through Google's native `generateContent` API instead of the
OpenAI-compatible endpoint:

```dotenv
MINIGENT_LLM_PROVIDER=google
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash
# Optional override; defaults to the Gemini API v1beta base.
GOOGLE_BASE_URL=https://generativelanguage.googleapis.com/v1beta
```

If Gemini requests fail with a warning like
`provider_request_failed ... exception_type='DecodingError'`, the response body was likely
advertised as compressed but could not be decoded by the HTTP client. This can happen with
corrupt or truncated compressed responses from an intermediary such as a proxy, VPN, or
security appliance. Disable response compression for Gemini by setting:

```dotenv
MINIGENT_LLM_EXTRA_HEADERS={"Accept-Encoding":"identity"}
```

For tenant execution config, use `llm.provider: "google"` and put the Gemini API key in
`llm.api_key`. Mindweft calls `POST /models/{model}:generateContent` and supports text
responses plus native Gemini function calls for Mindweft tools. Tool input schemas are sent
as `parametersJsonSchema` so Gemini can accept full JSON Schema from MCP tools.

## Anthropic LLM Provider

Mindweft can call Anthropic's native Messages API instead of routing Claude models through
OpenRouter or another OpenAI-compatible gateway:

```dotenv
MINIGENT_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your-anthropic-api-key
ANTHROPIC_MODEL=claude-haiku-4-5
# Optional overrides.
ANTHROPIC_BASE_URL=https://api.anthropic.com/v1
ANTHROPIC_MAX_TOKENS=4096
ANTHROPIC_VERSION=2023-06-01
# Optional Anthropic prompt caching. Enabled by default; set false to omit cache_control.
ANTHROPIC_PROMPT_CACHE_ENABLED=true
# Optional extended thinking / reasoning for supported Claude models.
ANTHROPIC_THINKING_ENABLED=true
ANTHROPIC_THINKING_BUDGET_TOKENS=1024
# Adaptive-thinking depth for Claude Opus/Sonnet 4.6+.
ANTHROPIC_THINKING_EFFORT=high
```

For tenant execution config, use `llm.provider: "anthropic"` and put the Anthropic API key
in `llm.api_key`. Mindweft calls `POST /messages`, maps Mindweft system messages to the
Messages API top-level `system` field, and supports text responses, image parts, and native
Anthropic tool-use/tool-result content blocks for Mindweft tools. Anthropic prompt caching
is enabled by default with top-level `cache_control: {"type":"ephemeral"}` so stable
prompt prefixes can be cached and Anthropic usage fields such as
`cache_read_input_tokens`/`cache_creation_input_tokens` are normalized to Mindweft's
`cache_read_tokens`/`cache_write_tokens`; set `ANTHROPIC_PROMPT_CACHE_ENABLED=false` to
omit `cache_control`. When Anthropic thinking is enabled, older Claude models receive
`thinking: {"type":"enabled","budget_tokens":...}`. Claude Opus/Sonnet 4.6 and newer,
including Claude Opus 4.8, instead receive the supported adaptive shape
`thinking: {"type":"adaptive","display":"summarized"}` with
`output_config: {"effort":"high"}` by default; set `ANTHROPIC_THINKING_EFFORT` to tune the
effort. Explicit `display: "summarized"` is required for visible thinking on Opus 4.8,
whose API default is `omitted`. Mindweft exposes returned `thinking` blocks through the
existing reasoning metadata/stream events and preserves Anthropic thinking blocks on
tool-use turns so they can be replayed with the tool result.

To inspect exactly which structural request and response paths the native API accepts,
without logging values by default, use:

```bash
# No key or network request required.
uv run python scripts/inspect_anthropic_shapes.py --dry-run --mode all

# Live adaptive-thinking request; reads ANTHROPIC_API_KEY from the process environment.
uv run python scripts/inspect_anthropic_shapes.py --model claude-opus-4-8 --mode adaptive
```

Add `--include-values` only when it is safe to display prompt, response, and thinking
content in the terminal.

## Generic OAuth LLM Provider

Mindweft can use a user-configured OAuth authorization-code + PKCE flow for LLM endpoints
that accept bearer tokens. There are no provider defaults; all OAuth and LLM parameters
must be supplied explicitly.

```dotenv
MINIGENT_LLM_PROVIDER=generic-oauth
MINIGENT_LLM_MODEL=your-model-id
MINIGENT_LLM_URL=https://provider.example/v1/responses
MINIGENT_LLM_EXTRA_HEADERS='{"x-provider-feature":"enabled"}'
# Optional: set a stable provider-side prompt-cache bucket for compatible Responses APIs.
# By default, generic-oauth uses the Mindweft thread ID, matching Codex's behavior.
# Set to a literal value to override, or to "thread"/"auto" to force thread-ID mode.
MINIGENT_LLM_PROMPT_CACHE_KEY=thread
# Optional: set when the provider requires an account/org header populated from a JWT claim.
MINIGENT_LLM_ACCOUNT_ID_HEADER=x-provider-account-id

MINIGENT_OAUTH_STORE_PATH=/path/to/oauth.db
# Configure either one 32-byte base64url key or a versioned keyring. A keyring supports rotation.
MINIGENT_OAUTH_ENCRYPTION_KEYS='{"1":"BASE64URL_32_BYTE_KEY"}'
MINIGENT_OAUTH_KEY_VERSION=1
# Optional one-time import source when migrating from the legacy JSON credential file.
MINIGENT_OAUTH_LEGACY_STORE_PATH=/path/to/oauth.json
MINIGENT_OAUTH_PROVIDER_ID=your-provider-id
MINIGENT_OAUTH_CLIENT_ID=your-client-id
MINIGENT_OAUTH_AUTHORIZE_URL=https://provider.example/oauth/authorize
MINIGENT_OAUTH_TOKEN_URL=https://provider.example/oauth/token
MINIGENT_OAUTH_REDIRECT_URI=http://127.0.0.1:8000/oauth/generic/callback
MINIGENT_OAUTH_SCOPE="openid profile offline_access"
MINIGENT_OAUTH_AUTH_PARAMS='{"prompt":"login"}'
# Optional: dot-separated path inside the access-token JWT, used with MINIGENT_LLM_ACCOUNT_ID_HEADER.
MINIGENT_OAUTH_ACCOUNT_ID_JWT_CLAIM=auth.account_id
```

The tenant-owner console can also import Pi's `openai-codex` OAuth credential from
`~/.pi/agent/auth.json`. The browser extracts only the `openai-codex` entry; Mindweft validates and
normalizes the Pi OAuth fields, encrypts them in the configured SQLite OAuth store, and keys them to
the authenticated tenant. The raw auth file and tokens are never returned by the API or included in
audit records.

Use **Tenant settings → Import from Pi**, acknowledge the rotating-refresh-token warning, and select
Pi's `auth.json`. Configure the tenant execution provider as `generic-oauth`, choose a Codex model,
and use `https://chatgpt.com/backend-api/codex/responses` as the LLM URL. The deployment's generic
OAuth client, token URL, and account-ID-header settings still apply to refreshes and requests.
Encrypted OAuth storage is mandatory for tenant imports.

Pi and Mindweft must not independently refresh copies of the same credential. OpenAI refresh-token
rotation can invalidate the other copy, so treat import as a credential transfer and avoid continuing
to use that Pi login concurrently.

Tenant-owner credential routes are:

```text
GET    /admin/tenants/{tenant_id}/oauth/openai-codex
POST   /admin/tenants/{tenant_id}/oauth/openai-codex/import/pi
DELETE /admin/tenants/{tenant_id}/oauth/openai-codex
```

The routes enforce active owner membership and same-tenant access. Import and deletion are audited
without credential material.

Start Mindweft, then begin login from a browser:

```text
http://127.0.0.1:8000/oauth/generic/open
```

Alternatively, `GET /oauth/generic/login` returns an `authorization_url` JSON field that
can be opened manually. When `MINIGENT_OAUTH_ENCRYPTION_KEY` or
`MINIGENT_OAUTH_ENCRYPTION_KEYS` is configured, the callback stores encrypted credentials and
single-use PKCE flow state in the SQLite database at `MINIGENT_OAUTH_STORE_PATH`. The
`generic-oauth` adapter coordinates refresh-token rotation through a transactional lease, so only
one replica refreshes an expired credential while other replicas reload the winner's result. The
same database lets a callback received by one replica consume a login flow started by another.

For migration, set `MINIGENT_OAUTH_LEGACY_STORE_PATH` to the previous JSON file. Its provider
entries are imported only once and never overwrite later database updates. Remove the legacy file
after verifying the import because that source remains plaintext. Versioned keys can be rotated by
starting with old and new keys present, re-encrypting stored rows with
`SQLiteEncryptedOAuthStore.reencrypt_to_active_key()`, and then retiring the old key.

Without an OAuth encryption key, Mindweft retains the compatibility JSON credential store and
in-memory login-flow store. JSON writes use atomic replacement, but that compatibility mode is not
safe for multiple replicas. Mindweft accepts callbacks at both `/oauth/generic/callback` and
`/auth/callback` for providers with fixed redirect path requirements.

For same-machine testing, start the API and open:

```text
http://127.0.0.1:8000/web/
```

For testing from another device on the same trusted network, bind Uvicorn to all
interfaces and visit the host machine's LAN address:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The browser client stores its base URL, auth settings, and current thread ID in browser
`localStorage`. If no bearer token is configured, it sends the same development
principal headers used by the CLI examples, so the default `dev-headers` auth mode works
for local testing. Use `static-tokens` or `jwt` before exposing the API outside a trusted
local network.

## Local Agent Wrapper POC

[`local-agent-wrapper`](/Users/burm/code/minigent/local-agent-wrapper) is a separate
minimal package that exposes a local coding-agent CLI as a federated-agent-style HTTP
member. It defaults to Pi Coding Agent and can be configured for OpenCode, Codex, or
another CLI with a custom argv template. Mindweft can route tasks to it through the
`peer_agent_task` tool when peer-agent tooling is enabled.

Run it locally with an explicit workspace allowlist:

```bash
cd local-agent-wrapper
uv sync --dev
AGENT_ALLOWED_WORKSPACES=/Users/burm/code/minigent \
  uv run uvicorn local_agent_wrapper.app:app --host 127.0.0.1 --port 8010
```

The POC supports `GET /agent-card`, `POST /tasks`, `GET /tasks/{task_id}`,
`GET /tasks/{task_id}/events`, read-only task artifact endpoints, and
`POST /tasks/{task_id}/cancel`. It runs `pi --mode json --no-session --tools read,grep,find,ls <prompt>` by default, parses
JSONL events when present, extracts Pi/OpenAI-style token usage when emitted by the
agent, falls back to stdout for `final_output`, and captures
stdout/stderr tails separately. With the
wrapper running, use `uv run python scripts/demo_task.py` from `local-agent-wrapper` for
a simple submit-and-poll demo. The demo prints `final_output` and hides the agent's
stderr/progress log unless `--show-log` is passed. Add `--show-events` to print parsed
JSON events. Set `AGENT_RUNTIME=opencode` for the built-in OpenCode profile,
`AGENT_RUNTIME=codex` for the built-in Codex profile, or use
`AGENT_ARGS_TEMPLATE` for another CLI. Task responses include relative `links` and
`artifacts` maps for discovery.

Mindweft can also use a configured peer agent as the primary thread execution backend
instead of the built-in LLM/tool loop. Start the wrapper, register it in Mindweft, and
select the `peer_agent` backend:

```dotenv
MINIGENT_PEER_AGENTS='[{"name":"pi","base_url":"http://127.0.0.1:8010"}]'
MINIGENT_AGENT_BACKEND=peer_agent
MINIGENT_AGENT_BACKEND_PEER=pi
MINIGENT_AGENT_BACKEND_CWD=/Users/burm/code/minigent
MINIGENT_MCP_BROKER_BASE_URL=http://127.0.0.1:8000
MINIGENT_MCP_BROKER_DB_PATH=/data/minigent-mcp-broker.db
MINIGENT_MCP_BROKER_ENABLED=true
```

With this mode, `POST /threads/{thread_id}/run` sends the Mindweft thread context to the
peer agent, including retained assistant tool-call records and tool results, polls until
the task completes, stores sanitized peer tool-execution events as retained tool-call/tool-result
messages, stores the peer `final_output` as the assistant message, and returns it as the run
reply. Streamed peer runs forward task `usage` on `peer.task.completed` when the peer reports
actual token counts. The peer-agent prompt protocol is currently text-only; Mindweft rejects
threads containing image parts instead of silently dropping those images. Per-tenant execution
config can use the same backend shape:

```json
{
  "agent_backend": {
    "type": "peer_agent",
    "peer": "opencode",
    "cwd": "/Users/burm/code/minigent",
    "timeout_seconds": 180,
    "poll_interval_seconds": 1,
    "mcp_broker_enabled": true
  }
}
```

The default backend remains `native`, which preserves the existing Mindweft LLM/tool
runtime.

## Remote Quality Enhancement

Mindweft can optionally ask a separate remote-quality model to critique a sanitized local
draft before the final assistant message is stored. The main runtime still produces the
initial answer with the tenant's normal LLM and tools. The quality path is advisory only:
Mindweft sends a redacted/minimized draft to the quality model, receives critique, then
asks the primary LLM to revise using the private thread context. Raw thread history, tool
outputs, and files are not sent to the quality model by this feature.

It is disabled by default. Enable it for env-based config with:

```dotenv
MINIGENT_REMOTE_QUALITY_ENABLED=true
MINIGENT_REMOTE_QUALITY_PROVIDER=openrouter
MINIGENT_REMOTE_QUALITY_MODEL=openai/gpt-5.4-mini
MINIGENT_REMOTE_QUALITY_API_KEY=...
MINIGENT_REMOTE_QUALITY_MODE=critique_draft
```

Per-tenant execution config can include the same shape:

```json
{
  "quality": {
    "enabled": true,
    "mode": "critique_draft",
    "provider": "openrouter",
    "model": "openai/gpt-5.4-mini",
    "api_key": "...",
    "max_payload_chars": 6000
  }
}
```

The sanitizer redacts common secrets, tokens, emails, private-network URLs, and absolute
paths, and can truncate the remote payload. This is a data-minimization guardrail, not a
mathematical privacy guarantee; keep the feature disabled for strict local-only use.

For a local llama.cpp demo with its OpenAI-compatible server listening on port 8080:

```bash
uv run python scripts/demo_local_quality.py \
  --llama-base-url http://127.0.0.1:8080/v1 \
  --llama-model local-model \
  --quality-provider mock
```

The `mock` quality provider exercises the sanitized critique path without requiring a
remote API key. Use `--quality-provider openrouter --quality-model ... --quality-api-key ...`
(or `openai`/`openai-compatible`) to demo a real remote reviewer. The script prints the
NDJSON run events, including `quality.sanitized`, `quality.remote_request`, and
`quality.applied` when the quality path is active.

The same local llama.cpp path is available as an opt-in integration test:

```bash
MINIGENT_RUN_LLAMA_CPP_INTEGRATION_TESTS=true \
  LLAMA_CPP_BASE_URL=http://127.0.0.1:8080/v1 \
  LLAMA_CPP_MODEL=local-model \
  uv run pytest tests/test_demo_local_quality_integration.py
```

Pi Coding Agent is the default peer profile. Install Pi separately and start the wrapper:

```bash
cd local-agent-wrapper
AGENT_ALLOWED_WORKSPACES=/Users/burm/code/minigent \
  uv run uvicorn local_agent_wrapper.app:app --host 127.0.0.1 --port 8010
```

The Pi profile invokes `pi --mode json --no-session --tools read,grep,find,ls <prompt>`
from the task workspace and extracts assistant `message_end` events as the task
`final_output`. This keeps the default Pi peer profile read-only. When Mindweft passes MCP
broker environment variables, the wrapper adds a generated Pi extension that registers
brokered Mindweft tools and activates them alongside the read-only file-inspection tools.
Those brokered tools are exposed to Pi with a sanitized `minigent_` prefix. Set
`AGENT_PI_TOOLS` as a comma-separated list to change the local Pi tools passed through
`--tools` while preserving automatic MCP broker extension injection. Override with
`AGENT_ARGS_TEMPLATE` when you want persistent Pi sessions, explicit
model/provider flags, fully custom tool narrowing, or custom Pi skills/extensions for a
specific peer deployment; a custom args template replaces the built-in Pi profile and
therefore must include any desired MCP extension wiring itself.

Set `MINIGENT_MCP_BROKER_ENABLED=false` or `agent_backend.mcp_broker_enabled=false` if
the peer agent should run without Mindweft-brokered MCP tools.

When the peer-agent backend runs, Mindweft mints a short-lived MCP broker session for
that thread and passes these environment variables to the wrapper task:

```dotenv
MINIGENT_MCP_BROKER_URL=http://127.0.0.1:8000/mcp/peer/<session>
MINIGENT_MCP_BROKER_TOKEN=<short-lived-token>
MINIGENT_MCP_BROKER_SESSION=<session>
```

The broker exposes the thread's approved Mindweft tools through MCP JSON-RPC and
forwards allowed `tools/call` requests through Mindweft's existing tool registry, so
OpenCode does not receive upstream MCP server credentials. The official SDK v2 low-level server
owns broker discovery, initialization, request validation, dispatch, and modern result envelopes;
Mindweft retains short-lived bearer authentication, session routing, frozen tool allowlists, and
execution context. Each authenticated HTTP request is adapted to an isolated SDK server exchange,
so modern stateless clients and the legacy initialization shape remain compatible. SDK JSON-RPC
models are also used for boundary errors, while modern-only result envelope fields are removed from
legacy responses. When
`MINIGENT_MCP_BROKER_DB_PATH` is configured, session identity, expiry, and the original approved
tool-name set are stored in shared SQLite. Bearer tokens are persisted only as SHA-256 hashes, and
a replica receiving a broker call reconstructs the tenant/thread tool registry locally while still
enforcing the tool names frozen when the session was created. This allows broker URLs minted by one
replica to be served by another. Without that setting, broker sessions remain process-local.
The wrapper only accepts task environment variables whose names start with
`MINIGENT_MCP_BROKER_` by default; override that allowlist with
`AGENT_ALLOWED_TASK_ENV_PREFIXES` if you add more task-scoped variables.

For OpenCode tasks, the wrapper also generates per-task `OPENCODE_CONFIG_CONTENT` that
adds a remote MCP server named `minigent` using the broker URL and bearer token. Existing
`OPENCODE_CONFIG_CONTENT` JSON is preserved and merged with the generated `mcp.minigent`
entry.

After starting both services with the peer-agent backend enabled, run a complete backend
smoke test with:

```bash
uv run python scripts/demo_opencode_backend.py \
  --message "Summarize this repository in one paragraph. Do not edit files."
```

For a Pi-backed peer-agent backend, use the matching demo:

```bash
uv run python scripts/demo_pi_backend.py \
  --message "Summarize this repository in one paragraph. Do not edit files."
```

To start the Pi wrapper, start Mindweft in peer-agent backend mode, and run that demo as
one local stack:

```bash
./scripts/demo_pi_backend_stack.sh
```

Pass a custom prompt as the first argument:

```bash
./scripts/demo_pi_backend_stack.sh "Summarize the local-agent-wrapper package. Do not edit files."
```

For an interactive development stack that keeps the Pi wrapper and Mindweft running
without launching a demo prompt, use:

```bash
./scripts/dev_pi_peer_stack.sh
```

By default, the wrapper allows tasks only in the Mindweft checkout and Mindweft sends peer
backend tasks with that same working directory. Override the target working directory with
`MINIGENT_PI_WORKSPACE`:

```bash
MINIGENT_PI_WORKSPACE=/Users/burm/code/some-project ./scripts/dev_pi_peer_stack.sh
```

If the wrapper should allow multiple roots, set `AGENT_ALLOWED_WORKSPACES` with the
platform path separator, for example `:` on macOS/Linux, while keeping
`MINIGENT_PI_WORKSPACE` as the task working directory:

```bash
MINIGENT_PI_WORKSPACE=/Users/burm/code/some-project \
AGENT_ALLOWED_WORKSPACES="/Users/burm/code/minigent:/Users/burm/code/some-project" \
  ./scripts/dev_pi_peer_stack.sh
```

Other useful overrides are `MINIGENT_HOST`, `MINIGENT_PORT`,
`MINIGENT_PI_WRAPPER_PORT`, `MINIGENT_PI_PEER_NAME`, `AGENT_COMMAND`,
`AGENT_RUNTIME`, and `AGENT_PI_TOOLS`. The development stack defaults
`AGENT_PI_TOOLS` to `read,grep,find,ls,write,edit,bash`.

To smoke-test brokered MCP tool use, run the demo for the configured peer:

```bash
uv run python scripts/demo_opencode_mcp_broker.py
uv run python scripts/demo_pi_mcp_broker.py
```

In the Mindweft API logs, look for `mcp_broker.tool_call` to confirm the peer called a
tool through the broker rather than only echoing the prompt text. The Pi stack script
enables the MCP broker by default; set `MINIGENT_MCP_BROKER_ENABLED=false` to disable it.

A Docker Compose variant is available for the Pi backend plus MCP broker path. It builds
the wrapper with `INSTALL_PI=true`, mounts this repository read-only, and keeps Pi state in
ignored `.pi-container/agent`. If `~/.pi/agent` exists, the script automatically copies it
into `.pi-container/agent` before starting the containers so host SSO/API-key logins are
available in the wrapper container. You can also prepare it explicitly:

```bash
./scripts/prepare-pi-container-home.sh
./scripts/demo_pi_backend_compose.sh
```

Or use common Pi API-key environment variables instead:

```bash
ANTHROPIC_API_KEY=... ./scripts/demo_pi_backend_compose.sh --no-prepare-pi-home
# or OPENAI_API_KEY=... ./scripts/demo_pi_backend_compose.sh --no-prepare-pi-home
```

Keep it running for inspection with `--keep-running`, then stop it with:

```bash
docker compose -f compose.pi-backend-demo.yaml down
```

The wrapper has opt-in real CLI integration tests:

```bash
cd local-agent-wrapper
MINIGENT_RUN_OPENCODE_INTEGRATION_TESTS=true uv run pytest tests/test_opencode_integration.py
MINIGENT_RUN_PI_INTEGRATION_TESTS=true uv run pytest tests/test_pi_integration.py
```

## Docker Compose Deployment

This repo now includes a production-oriented [`Dockerfile`](/Users/burm/code/minigent/Dockerfile)
and [`compose.yaml`](/Users/burm/code/minigent/compose.yaml) for running Mindweft on a remote
host that already manages apps with Docker Compose.

The runtime can persist thread state and message history in SQLite when
`MINDWEFT_THREAD_DB_PATH` points at a writable database path. SQLite-backed runs acquire an atomic
per-thread lease, heartbeat that lease while executing, and accept cancellation requests from any
replica. Expired leases are recovered as errored runs at startup and by a five-second background
sweep in every replica; run IDs fence late completion and message writes from stale owners. For a
peer-agent run, Mindweft generates a remote task ID and attaches it with the trusted peer base URL
to the lease before sending the create request. Compatible wrappers advertise
`task.idempotent_create`, atomically create at most one process for that ID, return the existing task
for an identical retry, reject conflicting reuse, and retain a canceled tombstone when cancellation
arrives before creation. Stale recovery can therefore cancel the known ID even if the owner fails
during task creation; a late create after cancellation returns `canceled` without starting a
process. Mindweft rejects a create response whose task ID differs from its reserved ID. Stale
recovery moves attached metadata into a durable cancellation outbox. User-requested cancellation,
backend timeout, ambiguous creation outcome, and nonterminal peer failures first attempt immediate
remote cancellation; if that request fails, the owner copies the attached metadata into the same
outbox before releasing its run lease. Replicas claim outbox entries with a lease, send idempotent
remote cancellation requests, and retry failures with exponential backoff. A claimant crash can
cause a repeated cancellation after its claim expires, so peer cancellation endpoints must remain
idempotent; only one replica can hold an active claim at a time. Upgrade peer wrappers before this
Mindweft version; older wrappers that ignore or replace client task IDs are rejected. Without SQLite
thread storage, threads and run coordination remain in memory and are lost on restart. The optional
admin control plane can also persist tenant execution config in SQLite when
`MINDWEFT_ADMIN_DB_PATH` points at a mounted volume.

The thread, attachment, rate-limit, OAuth, private-value, DAV, run-lease, and optional MCP broker stores support shared
replica state when their SQLite paths are configured. Keep every replica on the same shared volume
and configuration so broker tool registries can be reconstructed consistently.

Thread history is compacted in memory as conversations grow. Older turns are folded into
the thread summary and removed from the raw message list, so `GET /threads/{thread_id}/messages`
returns the retained recent tail instead of an unbounded full transcript. Tool-call turns
are stored as assistant messages with `tool_name`, `tool_call_id`, and `tool_arguments`,
followed by `tool` result messages. Peer-agent backend tool events are retained the same
way using sanitized event data: raw peer arguments are stripped, allowlisted argument
summaries may be stored in `tool_arguments.summary`, and sanitized result fields are stored
as the tool result content. For debugging the exact retained model-facing thread context,
`GET /threads/{thread_id}/context/raw` returns the current summary, raw retained messages,
a rendered transcript that includes tool calls/results, and the same estimated thread-context
token usage reported by streaming run events.

Start from [.env.template](/Users/burm/code/minigent/.env.template), then set at least:

```dotenv
MINDWEFT_AUTH_MODE=jwt
MINDWEFT_LLM_PROVIDER=openai
OPENAI_API_KEY=...
MINDWEFT_LOG_FORMAT=json
MINDWEFT_THREAD_DB_PATH=/data/mindweft-threads.db
MINDWEFT_ATTACHMENT_DB_PATH=/data/mindweft-attachments.db
MINDWEFT_RATE_LIMIT_DB_PATH=/data/mindweft-rate-limits.db
```

Rate limits use shared atomic token buckets for attachment uploads and thread runs. Tenant and user
buckets are configured independently with the `MINDWEFT_UPLOAD_RATE_LIMIT_*` and
`MINDWEFT_RUN_RATE_LIMIT_*` capacity/refill settings. Capacity `0` disables a bucket. A rejected
request returns HTTP 429 with `Retry-After`; standard and streaming runs share the same run category,
and binary and base64 attachment endpoints share the same upload category. Concurrent-run limits
use `MINDWEFT_RUN_CONCURRENCY_*` tenant/user capacities plus renewable expiry leases in the same
SQLite store, and also cover private-consent resumes. Configure the shared rate-limit database
whenever more than one replica is active.

Bring the service up with:

```bash
docker compose build
docker compose up -d
```

[`compose.yaml`](/Users/burm/code/minigent/compose.yaml) now reads the image name from
`MINDWEFT_IMAGE` (then legacy `MINIGENT_IMAGE`) and falls back to a local `mindweft:latest` tag.
The service and data-volume keys remain `minigent` so in-place upgrades retain Compose DNS,
operator commands, and existing volume data. Set the image in your
deployment environment before using a published image:

```dotenv
MINDWEFT_IMAGE=ghcr.io/<your-github-user-or-org>/mindweft:latest
```

If you want the host to run a published private image instead of building locally, log in
to GHCR first and then pull the image explicitly:

```bash
docker login ghcr.io
docker compose pull
docker compose up -d
```

You can still force a local rebuild from source at any time:

```bash
docker compose up -d --build
```

If you keep multiple deployment env files, point Compose at the one you want both for
variable interpolation and for the container environment itself:

```bash
MINDWEFT_ENV_FILE=.env.docker docker compose --env-file .env.docker up -d
```

`MINDWEFT_ENV_FILE` takes precedence over the legacy `MINIGENT_ENV_FILE` interpolation variable.

For the Dockerized setup used in this repo, prefer the wrapper script instead of typing
that command each time:

```bash
./scripts/docker-up.sh
```

Pass any extra `docker compose up` flags through to the script:

```bash
./scripts/docker-up.sh --build
```

## Private GHCR Image Publish

You can publish Mindweft to a private GHCR package even if this source repo is not hosted
on GitHub. GHCR only needs a GitHub user or organization namespace plus a token that can
write packages.

Log in with a GitHub personal access token that can push packages:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
```

Then set your package namespace once in `.env`:

```dotenv
IMAGE_NAMESPACE=<github-user-or-org>
```

Publish an image with the helper script in this repo:

```bash
IMAGE_TAG=latest ./scripts/docker-build-push.sh
```

Useful overrides can still be passed in the shell when they are not already set in `.env`:

```bash
IMAGE_TAG=sha-$(git rev-parse --short HEAD) \
PLATFORMS=linux/amd64,linux/arm64 \
./scripts/docker-build-push.sh
```

The script sources `.env` with `set -a` before applying defaults, then reads these environment variables:

- `IMAGE_NAMESPACE` (required): GitHub user or organization that owns the package
- `IMAGE_NAME` (default `mindweft`): package/image name
- `IMAGE_TAG` (default `latest`): image tag to push
- `PLATFORMS` (default `linux/amd64`): comma-separated buildx target platforms
- `REGISTRY` (default `ghcr.io`): registry hostname

For the Pi peer-agent wrapper image, use the matching helper script:

```bash
IMAGE_NAMESPACE=<github-user-or-org> \
IMAGE_TAG=latest \
./scripts/docker-build-push-pi-peer-agent.sh
```

It builds `local-agent-wrapper/Dockerfile` with Pi enabled and pushes
`ghcr.io/<namespace>/mindweft-local-agent-wrapper:<tag>` by default. It supports the same
`REGISTRY`, `IMAGE_NAMESPACE`, `IMAGE_NAME`, `IMAGE_TAG`, and `PLATFORMS` variables, plus
`INSTALL_PI` (default `true`) and `INSTALL_CODEX` (default `false`) build-arg overrides.

For remote deployments, set `MINDWEFT_IMAGE` in the deployment env file to the published
tag you want to run, then use `docker compose pull` followed by `docker compose up -d`.

[`compose.yaml`](/Users/burm/code/minigent/compose.yaml) uses whatever auth mode you set
in `.env`; it does not override `MINDWEFT_AUTH_MODE`. For local client testing,
`static-tokens` is the easiest path. For remote exposure, prefer `jwt` and include the
required JWT verification settings in `.env`.

By default, [`compose.yaml`](/Users/burm/code/minigent/compose.yaml) binds the API to
`127.0.0.1:8000` so a same-host reverse proxy can publish it safely. If you need direct
network exposure, change the port mapping deliberately instead of binding to all
interfaces by default.

The container exposes four unauthenticated health and lifecycle endpoints:

- `GET /health` remains a compatibility alias for the shallow process check.
- `GET /health/live` checks only that the API process can respond; temporary storage or dependency
  failures do not make liveness fail.
- `GET /health/ready` opens every configured SQLite-backed thread, MCP broker, private-value,
  consent, admin, and encrypted OAuth store in read/write mode and queries its schema. It returns
  `503` with per-store `ok`/`failed` states when any configured database is inaccessible. A draining
  process also returns `503` with a failed `lifecycle` check. Paths and raw exception details are not
  disclosed.
- `POST /health/drain` is restricted to loopback clients for container lifecycle hooks. It marks the
  process unready, rejects new runs with `503`, cancels active local run tasks, and waits for their
  cancellation handlers. Peer backends immediately request remote cancellation and durably enqueue
  a retry before releasing their run lease if that request fails. Drain is one-way for the process;
  restart it to accept runs again.

Kubernetes should use `/health/live` for liveness, `/health/ready` for readiness, and invoke
`POST /health/drain` from an exec-based `preStop` hook through `127.0.0.1`. Dependencies running as
sidecars should retain their own readiness probes so Kubernetes removes the whole pod from Service
endpoints when any required container is unready.

[`compose.yaml`](/Users/burm/code/minigent/compose.yaml) mounts a named volume at
`/data`, so `MINDWEFT_THREAD_DB_PATH=/data/mindweft-threads.db` survives container
restarts.

If you want the optional admin SQLite control plane too, add these settings to `.env`:

```dotenv
MINDWEFT_TENANT_CONFIG_SOURCE=store-with-defaults
MINDWEFT_ADMIN_DB_PATH=/data/mindweft-admin.db
MINDWEFT_ADMIN_ENCRYPTION_KEY=replace-with-a-long-random-secret
```

When `MINDWEFT_TENANT_CONFIG_SOURCE` is `store` or `store-with-defaults`,
`MINDWEFT_ADMIN_ENCRYPTION_KEY` is mandatory.

For the client as a normal CLI app, install the package with the `voice` extra so the
`mindweft-client` command is available on your `PATH`:

```bash
uv tool install '.[voice]'
mindweft-client stdin --wake-phrase "hey mindweft"
```

That installs an isolated tool environment and links the console scripts into uv's tool
bin directory. If the bin directory is not already on your `PATH`, run `uv tool dir
--bin` to find it.

For a remote Linux host that owns the microphone and speaker, use the installer script
over SSH:

```bash
ssh <user>@<host>
cd /path/to/mindweft
./scripts/install-client-linux.sh --systemd-user
```

The script installs Linux audio/build prerequisites, installs the package with the
`voice` extra, writes `.env.voice` if it does not already exist, checks ALSA devices,
and can install a `systemd --user` service. The canonical service is `mindweft-client`; the
installer disables and removes legacy `minigent-client`, `minigent-daemon`, and
`minigent-voice-daemon` unit files before enabling it. Edit `.env.voice` with the Mindweft API URL,
voice API token, and STT/TTS provider keys before starting passive audio in production.
The generated env file enables a bell-style wake acknowledgement with
`MINDWEFT_VOICE_WAKE_ACKNOWLEDGEMENT=bell`; add that setting manually if you already
had an env file before running the installer.

If the script adds your user to the `audio` group, log out and back in before starting
the client. Existing SSH sessions do not gain new group memberships automatically. You
can inspect the host first without installing anything:

```bash
./scripts/install-client-linux.sh --check-only
```

After editing `.env.voice`, run smoke tests in this order:

```bash
./scripts/run-client-linux.sh --backend stdin
./scripts/run-client-linux.sh --backend manual-audio --once
```

If you installed the user service, manage it with:

```bash
systemctl --user start mindweft-client
journalctl --user -u mindweft-client -f
```

Use `--enable-linger` with the installer when the user service should continue running
after the SSH user logs out:

```bash
./scripts/install-client-linux.sh --systemd-user --enable-linger
```

If the remote host is only running the Mindweft API and does not have the microphone,
run the client on the local machine with audio hardware and point
`MINDWEFT_BASE_URL` at the remote API instead.

If you want the installed tool to track your local checkout while you edit this repo, use
an editable tool install instead:

```bash
uv tool install --editable '.[voice]'
```

Use `--reinstall` with either form when you want uv to recreate the tool environment:

```bash
uv tool install --reinstall '.[voice]'
uv tool install --reinstall --editable '.[voice]'
```

If you are developing inside this repo instead, sync the optional voice dependencies into
the project virtualenv:

```bash
uv sync --dev --extra voice
```

For a complete CLI reference covering all commands, interactive slash commands, streaming
options, voice modes, and configuration, see [CLI reference](cli.md).

You can also enable local TTS on macOS with:

```bash
MINIGENT_VOICE_TTS_PROVIDER=say
MINIGENT_VOICE_TTS_VOICE=Samantha
mindweft-client manual-audio --once
```

With `MINIGENT_VOICE_TTS_PROVIDER=say`, passive mode also supports wake-word barge-in:
saying the wake word again while the assistant is speaking will stop `say` and switch
back to listening.

When local TTS is enabled, the client strips common Markdown formatting such as `*`, `` ` ``,
headers, lists, and Markdown links before feeding text to the speech engine, while still
printing the original assistant reply to the terminal. Structural Markdown like headers
and list items is converted into short sentence boundaries so TTS does not run them into
surrounding text. That includes `-`/`*` bullets, task-list checkboxes, and ordered lists
written as either `1.` or `1)`.

For higher-quality local TTS on macOS or Linux, install the voice extra and configure
Piper with a model path or model name:

```bash
MINIGENT_VOICE_TTS_PROVIDER=piper
MINIGENT_VOICE_TTS_MODEL=en_US-lessac-medium
mindweft-client manual-audio --once
```

For multi-speaker Piper models, also set `MINIGENT_VOICE_TTS_SPEAKER`:

```bash
MINIGENT_VOICE_TTS_PROVIDER=piper
MINIGENT_VOICE_TTS_MODEL=/absolute/path/to/voice.onnx
MINIGENT_VOICE_TTS_SPEAKER=0
mindweft-client manual-audio --once
```

`piper-tts` ships as part of the `voice` extra. When
`MINIGENT_VOICE_TTS_MODEL` is a bare voice name like `en_US-lessac-medium`, the client
downloads the `.onnx` and `.onnx.json` files on first use into
`~/.cache/minigent/piper` by default. Override that cache directory with
`MINIGENT_VOICE_TTS_MODEL_DIR` or `--tts-model-dir`. On macOS, Piper synthesis now plays
back through the native `afplay` command so wake-word barge-in does not fight the live
microphone PortAudio stream. On other platforms, Piper playback continues to use
`sounddevice`.

If the Mindweft API or upstream LLM returns a transient error during a voice turn, the
client logs the failure, optionally speaks a short local error message, and returns to
idle instead of exiting the process.

Piper uses `MINIGENT_VOICE_TTS_SENTENCE_SILENCE=0.35` by default so Markdown lists and
headers get an audible pause after they are converted into sentence boundaries. Increase
it if bullets still sound too continuous, or set it to `0` to disable the extra pause.
If the voice itself sounds too fast, set `MINIGENT_VOICE_TTS_LENGTH_SCALE` above `1.0`;
for example:

```bash
MINIGENT_VOICE_TTS_PROVIDER=piper
MINIGENT_VOICE_TTS_MODEL=en_US-lessac-medium
MINIGENT_VOICE_TTS_SENTENCE_SILENCE=0.55
MINIGENT_VOICE_TTS_LENGTH_SCALE=1.15
mindweft-client manual-audio --once
```

The client currently supports four backends:

- `chat`: plain terminal chat mode with direct stdin input and terminal replies, with no
  wake-word or audio pipeline
- `stdin`: text-driven wake phrase loop for cheap end-to-end testing
- `manual-audio`: press Enter to activate the microphone, record until silence using
  Silero VAD, transcribe the utterance with OpenAI or OpenRouter speech-to-text, then send the text
  into Mindweft and print the assistant reply
- `passive-audio`: continuously listen for a wake word, keep a short pre-roll audio
  buffer, then record until silence and transcribe through the same speech pipeline

`MINIGENT_VOICE_WAKE_PHRASE` is the text trigger for the `stdin` backend. `chat` does
not use wake-word processing at all. In `passive-audio`, the actual wake trigger comes
from the configured wake-word provider: `MINIGENT_VOICE_KEYWORD_PATH` for Porcupine or
`MINIGENT_VOICE_OWW_MODEL` for openWakeWord.

Examples:

```bash
mindweft-client chat

mindweft-client stdin --wake-phrase "hey mindweft"
# ignored
hello there
# activates and uses the rest of the line as the utterance
hey minigent summarize the latest thread
# or activate first, then provide the utterance on the next line
hey minigent
show me the transcript
```

If the client should consistently send client-owned prompt context with each utterance,
set `MINIGENT_VOICE_PROMPT_PREAMBLE`. The client prepends it to the prompt text it sends
to Mindweft; the server treats that as ordinary user content and does not validate or
infer anything on its own.

Example:

```bash
MINIGENT_VOICE_PROMPT_PREAMBLE='timezone=America/Chicago
note=prefer local context' \
mindweft-client manual-audio --once
```

For coarse location specifically, `MINIGENT_VOICE_LOCATION` remains available as a
compatibility convenience. When `MINIGENT_VOICE_PROMPT_PREAMBLE` is unset, the client
converts `MINIGENT_VOICE_LOCATION` into client context automatically. If both are set,
`MINIGENT_VOICE_PROMPT_PREAMBLE` wins.

For prompt-level diagnostics, set `MINIGENT_VOICE_DEBUG_SHOW_PROMPT=true`. The client
will print the exact outbound user message after any location prefix is added and before
it sends the request to Mindweft.

Manual audio example:

```bash
OPENAI_API_KEY=...
mindweft-client manual-audio --once
```

If you want voice input without spoken assistant playback, disable TTS and keep the
assistant reply in the terminal:

```bash
OPENAI_API_KEY=...
MINIGENT_VOICE_TTS_PROVIDER=none
mindweft-client manual-audio --once
```

Using OpenRouter for transcription:

```bash
OPENROUTER_API_KEY=...
MINIGENT_VOICE_STT_PROVIDER=openrouter
MINIGENT_VOICE_STT_MODEL=openai/gpt-audio
mindweft-client manual-audio --once
```

Using local faster-whisper transcription:

```bash
MINIGENT_VOICE_STT_PROVIDER=faster-whisper
MINIGENT_VOICE_STT_MODEL=base
MINIGENT_VOICE_STT_DEVICE=cpu
MINIGENT_VOICE_STT_COMPUTE_TYPE=int8
MINIGENT_VOICE_STT_LANGUAGE=en
mindweft-client manual-audio --once
```

In `manual-audio` mode, press Enter to start recording. The client stops recording after
trailing silence or `MINIGENT_VOICE_MAX_RECORD_SECONDS`, transcribes the utterance, and
then sends the transcript through the normal Mindweft thread/run flow.

The current speech-to-text providers are:

- `openai`: uses the `/audio/transcriptions` API
- `openrouter`: uses `/chat/completions` with `input_audio`
- `faster-whisper`: runs a local Whisper-family transcription model

For `openrouter`, choose a model that supports audio input. `openai/gpt-audio` is a good
starting point. The OpenAI-native transcription model ID `gpt-4o-mini-transcribe` is
for OpenAI's `/audio/transcriptions` API and is not a valid OpenRouter model ID.

For `faster-whisper`, `base` is a sensible starting point for command-style speech on a
laptop. `MINIGENT_VOICE_STT_DEVICE=cpu` and `MINIGENT_VOICE_STT_COMPUTE_TYPE=int8` are
good conservative defaults. For short English voice commands, set
`MINIGENT_VOICE_STT_LANGUAGE=en` instead of relying on auto-detection.

Passive wake-word example:

```bash
PICOVOICE_ACCESS_KEY=...
MINIGENT_VOICE_KEYWORD_PATH=/absolute/path/to/hey-minigent.ppn
mindweft-client passive-audio
```

If you keep the client settings in `.env.voice.docker`, use the wrapper script:

```bash
./scripts/client-docker.sh
```

It exports `.env.voice.docker` into the process environment, then runs:

```bash
mindweft-client passive-audio
```

Press `Ctrl-C` to stop the client cleanly. It will print `[idle] shutting down` and
exit without dumping a traceback from the audio backend.

Free `openwakeword` example:

```bash
MINIGENT_VOICE_WAKEWORD_PROVIDER=openwakeword
MINIGENT_VOICE_OWW_MODEL=okay_nabu
mindweft-client passive-audio
```

`passive-audio` keeps the microphone open, feeds chunks into the configured wake-word detector, and after the
wake word fires it prepends a short pre-roll buffer before recording the utterance to
reduce clipped first words.

Passive mode can also delay very briefly after wake detection before opening the fresh
recording stream, then add a small amount of leading and trailing silence before STT.
Those controls help make passive captures look more like the known-good manual capture
path when audio-capable chat models are sensitive to tightly cropped speech.

You can also configure an optional wake acknowledgement before recording starts. Set
`MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT=bell` for a short alert sound on macOS or Linux,
with a terminal bell fallback if no local player/default sound is available, or set it
to plain text such as `ready` to speak a short cue through the configured TTS provider.
To force a specific sound file, set `MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT_SOUND` to a
local audio file path.

You can also configure an optional cue after microphone capture ends and before STT
processing starts:

```bash
MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT=bell
```

Use `MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT_SOUND` to force a different sound
file for that end-of-capture cue.

If no speech arrives within `MINIGENT_VOICE_POST_WAKE_SPEECH_TIMEOUT_MS` after the wake
word, passive mode ignores that activation and returns to idle without sending audio to
STT.

To make short back-and-forth follow-ups feel more natural, set
`MINIGENT_VOICE_FOLLOW_UP_TIMEOUT_MS` to keep listening briefly after the assistant
finishes speaking. During that window, `passive-audio` accepts one follow-up utterance
without requiring the wake word, then returns to normal wake-word mode after silence.

On macOS, you can also lower ambient system output only while the client is actively
capturing an utterance or listening during that follow-up window:

```bash
MINIGENT_VOICE_DUCKING_MODE=input-only
MINIGENT_VOICE_DUCKED_OUTPUT_VOLUME=20
mindweft-client passive-audio
```

This is system-wide ducking, not per-app mixing. The client does not duck during idle
wake-word monitoring, thinking, or assistant speech. That keeps built-in TTS at your
normal output level, but it also means this feature does not literally duck every other
app while leaving the client isolated on the same output device.

If you need to inspect captured audio, set `MINIGENT_VOICE_DEBUG_CAPTURE_PATH` or pass
`--debug-capture-path`. The client will print capture metadata and write the last WAV
capture there before transcription. That is useful for comparing `manual-audio` and
`passive-audio` artifacts.

If you need to inspect the OpenRouter STT request/response payloads, set
`MINIGENT_VOICE_STT_DEBUG_PATH` or pass `--stt-debug-path`. The client and replay tool
will write debug artifacts such as `request.json` and `response.json` there.

When STT returns a bad assistant-style answer instead of a transcript, the client now
logs the failure, ignores that capture, and returns to idle instead of crashing.

If you want to experiment with audio level differences before STT, the replay tool also
supports `--gain`, `--normalize-peak`, `--pad-leading-ms`, and `--pad-trailing-ms`. That
is useful when comparing quieter passive captures against louder manual captures, or when
you want to approximate the passive client's STT padding against a saved WAV.

To replay a prerecorded capture through the STT adapters without involving the microphone
loop, use:

```bash
uv run python scripts/replay_stt.py /tmp/minigent-last-capture-manual.wav --metadata-only
uv run python scripts/replay_stt.py /tmp/minigent-last-capture-passive.wav --provider openrouter --stt-debug-path /tmp/minigent-stt-debug
uv run python scripts/replay_stt.py /tmp/minigent-last-capture-passive.wav --provider openrouter --normalize-peak
uv run python scripts/replay_stt.py /tmp/minigent-last-capture-passive.wav --provider openrouter --pad-leading-ms 250 --pad-trailing-ms 500
```

That is useful for comparing the exact passive/manual artifacts against the same STT
provider.

The current wake-word providers are:

- `porcupine`: stronger out-of-the-box wake-word path, requires `PICOVOICE_ACCESS_KEY`
- `openwakeword`: free local alternative using built-in `pyopen-wakeword` models such as `okay_nabu`

Daemon-related env vars:

- `MINIGENT_BASE_URL`
- `MINIGENT_VOICE_WAKE_PHRASE`
- `MINIGENT_VOICE_PROMPT_PREAMBLE`
- `MINIGENT_VOICE_LOCATION`
- `MINIGENT_VOICE_DEBUG_SHOW_PROMPT`
- `MINIGENT_CLIENT_STREAM_RUNS`
- `MINIGENT_CLIENT_SHOW_TOOL_RESULTS`
- `MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT`
- `MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT_SOUND`
- `MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT`
- `MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT_SOUND`
- `MINIGENT_VOICE_STT_PROVIDER`
- `MINIGENT_VOICE_STT_DEVICE`
- `MINIGENT_VOICE_STT_COMPUTE_TYPE`
- `MINIGENT_VOICE_STT_LANGUAGE`
- `MINIGENT_VOICE_TTS_PROVIDER`
- `MINIGENT_VOICE_TTS_VOICE`
- `MINIGENT_VOICE_TTS_MODEL`
- `MINIGENT_VOICE_TTS_MODEL_DIR`
- `MINIGENT_VOICE_TTS_SPEAKER`
- `MINIGENT_VOICE_TTS_LENGTH_SCALE`
- `MINIGENT_VOICE_TTS_SENTENCE_SILENCE`
- `MINIGENT_VOICE_WAKEWORD_PROVIDER`
- `MINIGENT_VOICE_SKILL`
- `MINIGENT_CLIENT_AGENT_PRESETS`
- `MINIGENT_VOICE_THREAD_ID`
- `MINIGENT_VOICE_AUDIO_DEVICE`
- `MINIGENT_VOICE_DEBUG_CAPTURE_PATH`
- `MINIGENT_VOICE_STT_DEBUG_PATH`
- `MINIGENT_VOICE_DUCKING_MODE`
- `MINIGENT_VOICE_DUCKED_OUTPUT_VOLUME`
- `MINIGENT_VOICE_AUDIO_SAMPLE_RATE`
- `MINIGENT_VOICE_AUDIO_BLOCK_SIZE`
- `MINIGENT_VOICE_END_SILENCE_MS`
- `MINIGENT_VOICE_MAX_RECORD_SECONDS`
- `MINIGENT_VOICE_FOLLOW_UP_TIMEOUT_MS`
- `MINIGENT_VOICE_POST_WAKE_SETTLE_MS`
- `MINIGENT_VOICE_WAKEWORD_PREROLL_MS`
- `MINIGENT_VOICE_STT_PAD_LEADING_MS`
- `MINIGENT_VOICE_STT_PAD_TRAILING_MS`
- `MINIGENT_VOICE_VAD_THRESHOLD`
- `MINIGENT_VOICE_STT_MODEL`
- `MINIGENT_VOICE_KEYWORD_PATH`
- `MINIGENT_VOICE_OWW_MODEL`
- `MINIGENT_VOICE_OWW_THRESHOLD`
- `MINIGENT_VOICE_API_TOKEN`
- `MINIGENT_VOICE_USER_ID`
- `MINIGENT_VOICE_TENANT_ID`
- `MINIGENT_VOICE_ADMIN`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENROUTER_API_KEY`
- `OPENROUTER_BASE_URL`
- `OPENROUTER_HTTP_REFERER`
- `OPENROUTER_APP_NAME`
- `PICOVOICE_ACCESS_KEY`

You can put provider settings in a local `.env` file. Start from [.env.template](/Users/burm/code/minigent/.env.template).

## Authentication

Authentication is controlled by `MINIGENT_AUTH_MODE`:

- `dev-headers`: trust canonical `X-Mindweft-*` headers for local development; legacy
  `X-Mindweft-*` headers remain accepted, with Mindweft values taking precedence
- `static-tokens`: resolve bearer tokens from `MINIGENT_AUTH_TOKENS`
- `jwt`: verify bearer JWTs and map claims into a `Principal`

### Generic Console Sessions

The production console can authenticate with deployment-managed static credentials without tying
Mindweft to a specific identity provider. This browser session layer works alongside any
`MINIGENT_AUTH_MODE`; bearer tokens and JWTs remain available for API clients.

Generate a scrypt password hash interactively (the password is not echoed or stored):

```bash
uv run python scripts/hash-session-password.py
```

Configure one or more usernames with hashed passwords and principals, plus an independent random
session-signing secret of at least 32 bytes:

```dotenv
MINIGENT_SESSION_CREDENTIALS={"admin":{"password_hash":"scrypt$16384$8$1$...","principal":{"user_id":"admin","tenant_id":"platform","is_admin":true}}}
MINIGENT_SESSION_SECRET=replace-with-at-least-32-random-bytes
MINIGENT_SESSION_TTL_SECONDS=28800
MINIGENT_SESSION_COOKIE_SECURE=true
MINIGENT_SESSION_ALLOWED_ORIGINS=https://minigent.example.com
```

`MINIGENT_SESSION_ALLOWED_ORIGINS` is optional and accepts a comma-separated list or JSON array.
The request's same origin is always allowed. Successful login sets the canonical
`mindweft_session` `HttpOnly`, `SameSite=Strict` cookie. Existing `minigent_session` cookies and
legacy `minigent-console` token issuers remain accepted during migration; canonical cookies take
precedence when both are present, and logout clears both names. Cookie-authenticated mutations
require a matching `Origin` header to prevent cross-site
request forgery. Passwords are verified with scrypt; store only generated hashes in deployment
secrets. Login attempts are limited per normalized username using the configured shared rate-limit
store; tune `MINIGENT_SESSION_LOGIN_RATE_LIMIT_CAPACITY` and
`MINIGENT_SESSION_LOGIN_RATE_LIMIT_REFILL_PER_SECOND` when needed. Login, status, and logout
use `POST`, `GET`, and `DELETE /auth/session`, respectively.

For a first-run administration deployment, also configure durable encrypted administration state
and allow console execution changes to override environment defaults:

```dotenv
MINDWEFT_ADMIN_DB_PATH=/data/mindweft-admin.db
MINDWEFT_ADMIN_ENCRYPTION_KEY=replace-with-a-long-random-secret
MINDWEFT_TENANT_CONFIG_SOURCE=store-with-defaults
```

After an `is_admin=true` credential signs in, an empty administration store presents the tenant
creation workflow. Create the initial tenant, membership, entitlements, and execution configuration
before using the workspace.

Deployment credentials are intended for bootstrap and break-glass administration. Additional tenant
users use local identities stored in the administration database:

1. Create an invited tenant user.
2. Select **Sign-in** beside that user and choose a globally unique login username.
3. Create a single-use setup link and deliver it to the user through a trusted channel.
4. The user opens the link and chooses a password of at least 12 characters. The URL keeps the raw
   token in its fragment so reverse proxies and HTTP access logs do not receive it.
5. Successful setup stores only a scrypt password hash, activates an invited membership, signs the
   user in, and permanently consumes the setup token.

Setup tokens are random, stored only as SHA-256 hashes, expire after 24 hours by default, and are
invalidated when a replacement link is generated. Administrators can create password-reset links or
disable local sign-in from the same user dialog. Password replacement and credential disabling bump
a credential version, immediately invalidating existing signed sessions. Local sessions also check
current tenant and membership status on every authenticated request, so suspension, archival, or
deletion takes effect without waiting for cookie expiry. Local tenant roles do not grant global
`is_admin` access; control-plane administration remains restricted to explicitly configured
bootstrap administrators.

The corresponding API routes are:

- `GET /admin/tenants/{tenant_id}/users/{user_record_id}/credential`
- `POST /admin/tenants/{tenant_id}/users/{user_record_id}/credential/setup`
- `DELETE /admin/tenants/{tenant_id}/users/{user_record_id}/credential`
- `POST /auth/password/setup/status`
- `POST /auth/password/setup`

### JWT Mode

Production-oriented mode uses JWT verification:

```dotenv
MINIGENT_AUTH_MODE=jwt
MINIGENT_JWT_ISSUER=https://issuer.example
MINIGENT_JWT_AUDIENCE=minigent-api
MINIGENT_JWT_ALGORITHMS=["RS256"]
MINIGENT_JWT_JWKS_URL=https://issuer.example/.well-known/jwks.json
```

If `MINIGENT_AUTH_MODE=jwt` is set without either `MINIGENT_JWT_SHARED_SECRET` for HMAC
algorithms or `MINIGENT_JWT_JWKS_URL` for asymmetric algorithms, the server now fails at
startup with a configuration error instead of returning `500 Internal Server Error` from
authenticated endpoints.

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
X-Mindweft-User-Id: user-123
X-Mindweft-Tenant-Id: tenant-abc
X-Mindweft-Admin: false
```

Thread lifecycle endpoints require the auth material for the active mode. Threads are isolated by `tenant_id`, and cross-tenant access returns `404`.

`GET /tenant-context` returns the resolved tenant context for the authenticated caller. When the tenant registry is not required, the response always includes the principal and `tenant_id`; if an admin store is enabled and the tenant exists, Mindweft enriches the context with registry fields, entitlement `features`, entitlement `limits`, and `entitlements_version`. When `MINIGENT_TENANT_REGISTRY_REQUIRED=true`, the endpoint and thread lifecycle endpoints require an active registry tenant. When `MINIGENT_TENANT_USER_REGISTRY_REQUIRED=true`, those paths also require an active tenant membership for the authenticated `user_id` and expose optional membership fields on the tenant context.

## Runtime Settings

`MINIGENT_MAX_ITERATIONS` controls the maximum number of LLM loop passes for one
`POST /threads/{thread_id}/run` call. The default is `16`.

This count includes tool-call passes and the final assistant-response pass, so a run that
uses tools can make at most one fewer tool call than the configured value. Increase it
for deeper MCP, retrieval, or verification workflows; decrease it when you want a tighter
runaway-loop guard.

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
  },
  "tenant-3":{
    "llm":{
      "provider":"google",
      "model":"gemini-3.5-flash",
      "api_key":"tenant-3-gemini-key"
    }
  }
}
```

Supported fields:

- `llm.provider`: `mock`, `openai`, `openrouter`, `openai-compatible`, `generic-oauth`, or `google`
- `llm.model`, `llm.base_url`, `llm.api_key`, `llm.extra_headers`, `llm.timeout`
- `tools.allowed_local_tools`: local tool allowlist
- `tools.mcp_servers`: per-tenant MCP server definitions
- `skills.default_skill`, `skills.items`: available prompt-overlay skills
- `capability_profiles.default_profile`, `capability_profiles.items`: explicit tool/MCP narrowing profiles
- `agents.default_agent`, `agents.items`: named server-side presets that combine skills and capability profiles for clients; the default agent supplies missing skill/profile selections when a thread is created

String values in `MINIGENT_TENANT_EXECUTION_CONFIGS` can reference environment values with
`${NAME}` placeholders. Placeholder replacement is recursive across nested objects and arrays,
and non-string JSON values are left unchanged. Missing variables expand to an empty string.
This also applies when the JSON is loaded through `MINIGENT_TENANT_EXECUTION_CONFIGS_FILE`.

For a developer-oriented example that combines multiple skills with explicit capability profiles,
see the commented block in [.env.template](/Users/burm/code/minigent/.env.template).

#### Agent Skill instruction sources

Mindweft skills can either define native `system_prompt` text or point to a local
Claude/Agent Skill `SKILL.md` file with `instruction_source`. Agent Skill sources preserve
progressive disclosure: only metadata such as `name` and `description` is part of the skill
catalog, and the `SKILL.md` body is read only when that Mindweft skill is selected or used as
the default skill for a thread.

```json
{
  "demo-tenant": {
    "skills": {
      "items": [
        {
          "name": "code-reviewer",
          "description": "Reviews code changes for correctness and maintainability.",
          "instruction_source": {
            "type": "agent_skill",
            "path": "/opt/minigent/skills/code-reviewer/SKILL.md"
          }
        }
      ]
    }
  }
}
```

The Agent Skill body becomes active instructions only for selected/default skills. Supporting
files such as `references/`, `scripts/`, and `assets/` are not loaded or executed
automatically. Tool permissions still come from tenant tool config and capability profiles;
Agent Skill metadata such as `allowed-tools` must not be treated as a permission grant.

### Coding workspace access

Coding workspace setup, including the reusable runner, filesystem MCP bridge, and the
recommended local-tool-vs-MCP boundary, is documented in [Coding workspace setup](coding-workspace.md).

In short: keep built-in Mindweft local tools for generic low-risk utilities. Expose
workspace-specific capabilities such as filesystem access, editing, shell commands, tests,
builds, and git operations through explicit MCP servers and capability profiles. The default
coding runner profile remains read-only (`inspect`); shell access should be a separate trusted
local MCP capability, not a default Mindweft local tool.

The local tool `retrieve_knowledge` is not enabled by default because it requires a
MiniRAG database and related backend setup. Enable it explicitly with
`tools.allowed_local_tools` (or a skill/capability profile allowlist), install the
MiniRAG package in the runtime environment, and set `MINIGENT_MINIRAG_DB_PATH` to a
SQLite database created by `minirag ingest`.

Recommended setup today:

- `MINIGENT_MINIRAG_BACKEND=dense`
- `MINIGENT_MINIRAG_EMBEDDING_PROVIDER=openrouter`

That matches the current best-performing `minirag` configuration on the FIQA external slices run so far.

Optional retrieval tuning env vars:

- `MINIGENT_MINIRAG_BACKEND`: `lexical`, `dense`, or `hybrid`
- `MINIGENT_MINIRAG_EMBEDDING_PROVIDER`: `hash`, `openai`, or `openrouter`
- `MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT`: optional lexical score weight for `hybrid`
- `MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT`: optional dense score weight for `hybrid`

For local development with `uv`, install the MiniRAG package into the environment you use
for Mindweft before enabling `retrieve_knowledge`.

Example:

```bash
export MINIGENT_MINIRAG_BACKEND=dense
export MINIGENT_MINIRAG_EMBEDDING_PROVIDER=openrouter
export OPENROUTER_API_KEY=...
```

If you want to tune `hybrid` explicitly:

```bash
export MINIGENT_MINIRAG_BACKEND=hybrid
export MINIGENT_MINIRAG_EMBEDDING_PROVIDER=openrouter
export MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT=0.05
export MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT=0.95
```

In `store-with-defaults`, a `*` tenant record in the admin store acts as a default profile before env fallback is considered.

## Admin API

The admin API is an authenticated control plane for the tenant registry, tenant execution config, and thread inspection. Tenant registry and execution config storage are backed by the admin SQLite database.

Enable tenant execution config storage with:

```dotenv
MINIGENT_ADMIN_DB_PATH=.data/minigent-admin.db
MINDWEFT_ADMIN_ENCRYPTION_KEY=replace-with-a-long-random-secret
# Optional: require request tenant IDs to exist and be active in the registry.
MINIGENT_TENANT_REGISTRY_REQUIRED=false
# Optional: require authenticated users to have active tenant membership.
MINIGENT_TENANT_USER_REGISTRY_REQUIRED=false
```

Admin endpoints:

- `GET /admin/tenants`
- `POST /admin/tenants`
- `GET /admin/tenants/{tenant_id}`
- `PATCH /admin/tenants/{tenant_id}`
- `POST /admin/tenants/{tenant_id}/activate`
- `POST /admin/tenants/{tenant_id}/suspend`
- `POST /admin/tenants/{tenant_id}/archive`
- `DELETE /admin/tenants/{tenant_id}`
- `POST /admin/tenants/seed`
- `GET /admin/tenants/{tenant_id}/users`
- `POST /admin/tenants/{tenant_id}/users`
- `GET /admin/tenants/{tenant_id}/users/{user_record_id}`
- `PATCH /admin/tenants/{tenant_id}/users/{user_record_id}`
- `POST /admin/tenants/{tenant_id}/users/{user_record_id}/activate`
- `POST /admin/tenants/{tenant_id}/users/{user_record_id}/suspend`
- `DELETE /admin/tenants/{tenant_id}/users/{user_record_id}`
- `GET /admin/tenants/{tenant_id}/entitlements`
- `PUT /admin/tenants/{tenant_id}/entitlements`
- `POST /admin/tenants/{tenant_id}/entitlements/validate`
- `DELETE /admin/tenants/{tenant_id}/entitlements`
- `GET /admin/execution-config-tenants`
- `GET /admin/tenants/{tenant_id}/attachments/statistics`
- `GET /admin/tenants/{tenant_id}/run-concurrency`
- `GET /admin/tenants/{tenant_id}/threads`
- `GET /admin/tenants/{tenant_id}/threads/{thread_id}`
- `DELETE /admin/tenants/{tenant_id}/threads/{thread_id}`
- `POST /admin/tenants/{tenant_id}/threads/prune`
- `GET /admin/tenants/{tenant_id}/audit-records`
- `GET /admin/tenants/{tenant_id}/execution-config`
- `PUT /admin/tenants/{tenant_id}/execution-config`
- `POST /admin/tenants/{tenant_id}/execution-config/validate`
- `DELETE /admin/tenants/{tenant_id}/execution-config`

Admin access requires an authenticated principal with `is_admin=true`. In `dev-headers` mode that means:

```bash
X-Mindweft-User-Id: admin-user
X-Mindweft-Tenant-Id: admin-tenant
X-Mindweft-Admin: true
```

Tenant registry endpoints manage durable tenant identity and lifecycle state. Tenant records include `id`, `slug`, `name`, `status`, `plan`, `region`, JSON `metadata`, actor fields, and timestamps. Slugs must be unique and contain lowercase letters,
digits, and hyphens. `DELETE /admin/tenants/{tenant_id}` soft-deletes by setting `status` to `deleted`;
it does not remove threads or execution config. `GET /admin/tenants` returns tenant objects with
pagination metadata and accepts `limit`, `offset`, `status`, `plan`, and `slug` query parameters.
`POST /admin/tenants/seed` can create missing registry tenants from existing execution-config tenant
IDs; pass `dry_run=true` to preview. Tenant entitlements are stored as `features` and `limits` JSON
objects with a monotonically increasing `version`; validation enforces scalar shape and the runtime's
known non-negative integer limits, and thread/message/run limits plus MCP/peer-agent feature gates are
enforced at runtime. `GET /admin/execution-config-tenants` preserves the old execution-config tenant
listing by returning tenant IDs that have stored execution config.

Execution-config reads and writes return the store's monotonically increasing `version`. API keys and
header values are returned as `<redacted>` markers with `has_*` metadata; validation and update
requests can round-trip those markers to preserve the corresponding stored values without
sending secrets to the browser. Supplying a new value replaces a secret, while explicit `null` clears
an API key. Updates preserve the submitted supported shape, including LLM profiles, MCP policy,
skills, capability profiles, and agent presets, rather than collapsing it to a partial normalized
payload. Successful updates and deletes invalidate the runtime resolver and append redacted tenant
audit records.

When `MINIGENT_TENANT_REGISTRY_REQUIRED=true`, public thread endpoints reject authenticated principals whose `tenant_id` is missing from the registry or not `active`. The default is `false` to preserve local and migration workflows. When `MINIGENT_TENANT_USER_REGISTRY_REQUIRED=true`, request-time tenant context resolution also requires an active tenant membership for `(tenant_id, user_id)` and populates membership fields such as `membership_id`, `user_role`, and `user_status` on `TenantContext`.

Thread inspection endpoints use the active thread store and are tenant-scoped by the `{tenant_id}` path parameter. The list endpoint returns metadata, message counts, and pagination metadata (`limit`, `offset`, `total`, `next_offset`). It accepts `limit`, `offset`, `status`, `profile`, `skill`, `created_after`, and `updated_after` query parameters. The detail endpoint returns metadata, compacted context state, and messages for one thread. Admin deletion removes a thread and its messages and writes an audit record. The prune endpoint deletes matching tenant threads with `updated_at` older than required `updated_before`, with optional `status`, `profile`, and `skill` filters. Add `dry_run=true` to preview `candidate_thread_ids` without deleting threads or writing audit records. The audit endpoint lists deletion/prune records and tenant mutation records with actor, action, affected count, thread IDs, optional `resource_type`/`resource_id`, optional `old_values`/`new_values`, optional metadata, timestamp, and pagination metadata (`limit`, `offset`, `total`, `next_offset`). It accepts `limit`, `offset`, `action`, `actor`, `created_after`, and `created_before` query parameters. Tenant audit payloads redact secret-like keys such as `token`, `secret`, `key`, `authorization`, and `password`. With `MINIGENT_THREAD_DB_PATH` configured, these endpoints can inspect and manage persisted threads and audit records after process restarts.

The React administration workspace exposes these thread operations with status/profile/skill/date
filters, tenant-scoped detail views for compacted context and retained messages, a dry-run preview
before filtered pruning, explicit confirmation before individual deletion, and action/actor/date
filters for the paginated audit log. Audit detail expansion displays only the already-redacted values
returned by the admin API.

The attachment statistics endpoint returns only tenant-level counts and byte totals split across pending, referenced, and lifecycle-exempt records, plus the oldest pending timestamp and age and the configured tenant quota. It does not read or return attachment contents or per-record metadata. The run-concurrency endpoint returns only aggregate active-run and active-user counts, the next lease expiration, and configured capacities/timings; it does not expose user IDs, thread IDs, or lease IDs.

The packaged CLI can inspect and manage the same tenant registry and thread data when authenticated as an admin:

```bash
mindweft --admin admin tenants list --status active --limit 50
mindweft --admin admin tenants create --id TENANT_ID --slug tenant-slug --name "Tenant Name" --status active
mindweft --admin admin tenants show TENANT_ID
mindweft --admin admin tenants update TENANT_ID --plan pro --metadata-json '{"owner":"support"}'
mindweft --admin admin tenants suspend TENANT_ID
mindweft --admin admin tenants activate TENANT_ID
mindweft --admin admin tenants archive TENANT_ID
mindweft --admin admin tenants delete TENANT_ID
mindweft --admin admin tenants seed --from execution-configs --status active --dry-run
mindweft --admin admin tenants seed --from execution-configs --status active
mindweft --admin admin tenants users list TENANT_ID --status active
mindweft --admin admin tenants users create TENANT_ID --user-id USER_ID --email user@example.com --role member --status active
mindweft --admin admin tenants users show TENANT_ID USER_RECORD_ID
mindweft --admin admin tenants users update TENANT_ID USER_RECORD_ID --role admin
mindweft --admin admin tenants users suspend TENANT_ID USER_RECORD_ID
mindweft --admin admin tenants users activate TENANT_ID USER_RECORD_ID
mindweft --admin admin tenants users delete TENANT_ID USER_RECORD_ID
mindweft --admin admin tenants entitlements show TENANT_ID
mindweft --admin admin tenants entitlements set TENANT_ID --features-json '{"mcp":true}' --limits-json '{"max_threads":100}'
mindweft --admin admin tenants entitlements validate TENANT_ID --features-json '{"mcp":true}'
mindweft --admin admin tenants entitlements delete TENANT_ID
mindweft --admin admin execution-config validate-file tenant-config.json
mindweft --admin admin execution-config import tenant-config.json --dry-run
mindweft --admin admin execution-config import tenant-config.json --upsert --seed-tenants
mindweft --admin admin execution-config export --out tenant-config.redacted.json
mindweft --admin admin execution-config export --tenant TENANT_ID
mindweft --admin admin threads list --tenant TENANT_ID --limit 50
mindweft --admin admin threads list --tenant TENANT_ID --status idle --profile default --skill coding
mindweft --admin admin threads show THREAD_ID --tenant TENANT_ID
mindweft --admin admin threads delete THREAD_ID --tenant TENANT_ID
mindweft --admin admin threads prune --tenant TENANT_ID --updated-before 2026-05-01T00:00:00Z
mindweft --admin admin threads prune --tenant TENANT_ID --updated-before 2026-05-01T00:00:00Z --dry-run
mindweft --admin admin audit list --tenant TENANT_ID --limit 50
mindweft --admin admin audit list --tenant TENANT_ID --action threads.prune --actor admin-user --created-after 2026-05-01T00:00:00Z
mindweft --api-token ADMIN_TOKEN admin threads list --tenant TENANT_ID --json
```

Secrets such as LLM API keys and MCP headers are accepted on writes but redacted in read responses. If `MINIGENT_TENANT_CONFIG_SOURCE` is `store` or `store-with-defaults`, `MINIGENT_ADMIN_ENCRYPTION_KEY` is required and those secrets are encrypted before being written to SQLite. Updating or deleting a tenant config invalidates the in-process execution cache for that tenant so new runs pick up the change immediately.

`MINIGENT_ADMIN_MCP_SERVER_CATALOG` optionally configures the internal-service quick-add cards shown
by the tenant execution editor. `MINIGENT_ADMIN_MCP_SERVER_CATALOG_SECRET` has the same format and
takes precedence; use it when catalog templates contain credential headers so secret-management
systems recognize the value as sensitive. It is a JSON array; each item has `id`, `title`,
`description`, an optional `detail`, and a `server` object containing the tenant MCP server
definition. The catalog definitions are deployment-owned. Platform administrators set each
tenant's maximum subset with `PUT /admin/tenants/{tenant_id}/mcp-server-catalog-policy`; the request
contains `item_ids`, `allow_custom_mcp_servers`, and optional `require_subject_assignment`.
`GET` returns the stored policy and `DELETE` restores the backward-compatible unmanaged behavior.
In unmanaged mode, all deployment catalog entries are visible and custom MCP servers remain
allowed. `GET /admin/mcp-server-catalog` returns the complete redacted deployment catalog to
platform administrators, while
`GET /admin/tenants/{tenant_id}/mcp-server-catalog` returns only that tenant's assigned entries and
includes `managed` and `allow_custom_mcp_servers` policy indicators.

Once a tenant policy exists, platform administrators can narrow MCP access by role or individual
user in the execution editor or through
`PUT /admin/tenants/{tenant_id}/mcp-server-catalog-assignments/{subject_type}/{subject_id}`. The
subject type is `role` (`owner`, `admin`, `member`, or `viewer`) or `user` (a tenant `user_id`), and
the request contains `item_ids`. An individual user assignment takes precedence over a role
assignment, and the selected assignment is intersected with the tenant policy. By default, a subject
without either assignment inherits tenant access; a saved empty assignment revokes every catalog
MCP server for that subject. When `require_subject_assignment=true`, an unassigned subject receives
no managed MCP servers. Suspended and deleted users always receive none. Enabling fail-closed mode
requires a non-empty active owner or admin user/role assignment within the tenant ceiling, preventing
accidental removal of all break-glass access.

`GET /admin/tenants/{tenant_id}/mcp-server-catalog-access-preview` reports each tenant user's
prospective effective source and item IDs; pass `require_subject_assignment=true` to dry-run
fail-closed enforcement before saving it. `GET
/admin/tenants/{tenant_id}/mcp-server-catalog-assignments` lists assignments, and deleting one
restores inheritance when fail-closed mode is disabled or denies access when it is enabled.
Assignment writes and deletes are audited. Runtime authorization is evaluated for every run, so
changes do not depend on rebuilding the tenant execution configuration.

Managed policies are enforced when execution configurations are validated or saved and again when
the store-backed runtime resolves a tenant. Catalog entries must be assigned, must retain their
catalog URL, and cannot expand `allowed_tools` beyond the catalog definition. When custom MCP
servers are disabled, non-catalog servers are rejected. Policy changes are audited and invalidate
the tenant execution cache, so revoking an entry prevents subsequent runs even if an older stored
execution configuration still references it.

`MINIGENT_ADMIN_EXTERNAL_GRANT_PROVIDERS` optionally enables a provider-neutral external grant
control plane for platform administrators. When unset, no providers or grant panels are registered.
The setting is a JSON array whose entries define `id`, `title`, optional `description`, `base_url`,
forwarded-identity `audience`, non-empty `read_scopes` and `write_scopes`, and
`allowed_permissions`. Generic endpoint defaults are `GET/PUT /v1/resource-grants` and
`DELETE /v1/resource-grants/{resource_id}?user_id=...`; deployments can override `list_path`,
`upsert_path`, and `delete_path`. Providers with a safe administrative resource catalog can set
optional `resources_path`; Mindweft uses it to show labels and resource-specific permissions while
preserving free-form entry for providers without discovery. Providers with an immutable audit API
can also set optional `audit_path`; Mindweft forwards `limit` and `before_id`, reads the provider
history with the configured read scopes, and displays it alongside current grants. URLs are
deployment-owned and reject embedded credentials, query strings, and non-HTTP schemes.

The platform-admin-only routes are `GET /admin/external-grant-providers`,
`GET/PUT /admin/tenants/{tenant_id}/external-grants/{provider_id}`,
`GET /admin/tenants/{tenant_id}/external-grants/{provider_id}/resources`,
`GET /admin/tenants/{tenant_id}/external-grants/{provider_id}/audit`, and
`DELETE /admin/tenants/{tenant_id}/external-grants/{provider_id}/{resource_id}?subject_id=...`.
The browser grant panel joins provider resources and grants with Mindweft's tenant-user directory.
New exact-subject grants can target only active tenant users; `*` is presented explicitly as
"Everyone in tenant." The API independently rejects enabled grants for missing or inactive tenant
users. Existing grants for missing or inactive users may still be disabled or deleted, allowing the
panel's reconciliation report and confirmed bulk-disable action to deprovision stale access without
silently mutating provider state. User labels and emails remain in Mindweft's platform-admin UI;
only canonical user IDs are forwarded to providers.

Changing an existing tenant user to `suspended` or `deleted` atomically appends a durable
user-deprovisioning event in the admin database. Runtime identity and MCP checks continue to deny
the inactive user immediately; provider availability is not part of that transaction. A background
worker claims due events with a SQLite lease so replicas do not process the same event concurrently,
removes the user's explicit MCP catalog assignment, and disables every enabled exact-subject grant
returned by each configured provider. Processing is idempotent: retries skip absent assignments and
already-disabled grants. Reactivating a user does not cancel pending deprovisioning or restore grants
or assignments; those require explicit administrative action.

Failures use exponential backoff and become `dead_letter` after eight attempts by default. Configure
polling, retry limits, and stale-claim recovery with
`MINIGENT_USER_DEPROVISIONING_INTERVAL_SECONDS`,
`MINIGENT_USER_DEPROVISIONING_MAX_ATTEMPTS`, and
`MINIGENT_USER_DEPROVISIONING_LEASE_SECONDS`. Tenant owners and platform administrators can inspect
`GET /admin/tenants/{tenant_id}/user-deprovisioning-events` (optionally filtered by `state`) and
requeue pending or dead-letter work with
`POST /admin/tenants/{tenant_id}/user-deprovisioning-events/{event_id}/retry`. Events retain the
initiating actor, target status, attempt count, redacted failure summary, assignment-cleanup result,
and count of grants disabled. The outbox is part of normal admin-database backup and restore.

Mindweft issues a fresh 30–300 second forwarded-identity token for each provider request and uses
only the configured read or write scopes. Provider credentials, scopes, and HTTP operations are
never added to tenant execution configuration or exposed as model tools. Grant state remains
authoritative at the provider; Mindweft stores only its normal redacted administrative audit
record. Provider availability is deliberately excluded from startup, readiness, chat execution,
and tool discovery, so an outage affects only grant-administration requests.

Example:

```bash
MINIGENT_ADMIN_EXTERNAL_GRANT_PROVIDERS='[{"id":"example-grants","title":"Example grants","description":"Manage authoritative external grants.","base_url":"http://127.0.0.1:8769","audience":"example-grants","read_scopes":["grants:read"],"write_scopes":["grants:write"],"allowed_permissions":["read","read_write"],"resources_path":"/v1/resources","audit_path":"/v1/resource-grant-audit"}]'
```

Header values are redacted before catalog
entries reach the browser. When a quick-add entry is validated or saved, its `<redacted>` header
placeholders are restored server-side from the deployment catalog; existing tenant values take
precedence. Therefore catalogs containing credentials must be supplied through a secret manager,
not committed as plaintext. `${NAME}` placeholders in catalog strings are resolved from the
process environment at startup, allowing a catalog stored in a secret to reference another secret
key such as `NETWISE_API_TOKEN`. For example:

```bash
MINIGENT_ADMIN_MCP_SERVER_CATALOG='[{"id":"web-search","title":"Web search","description":"Search web, news, and ranked page content.","detail":"Local Brave Search sidecar · 3 tools","server":{"name":"web-search","url":"http://127.0.0.1:8766/mcp","headers":{},"allowed_tools":["brave_web_search","brave_news_search","brave_llm_context"]}}]'
```

The admin CLI can bridge the static JSON and DB-backed modes. `admin execution-config import`
accepts the same top-level tenant map used by `MINIGENT_TENANT_EXECUTION_CONFIGS`; it also
accepts a bundle with an `execution_configs` object. Imports validate each tenant before any
write; use `--dry-run` to preview and `--upsert` to write valid configs. `--seed-tenants` can
create missing registry records after a successful import. `admin execution-config export`
uses the admin API and therefore writes redacted JSON suitable for review, diffs, and
migration checks, not a full secret-bearing backup.

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
Mindweft defaults new server entries to MCP `2026-07-28`: the outbound client uses the
official MCP Python SDK v2 to probe `server/discover`, validate schemas/results, and fall back
to the `2025-11-25` `initialize` handshake when discovery is rejected. Mindweft wraps the SDK
with allowed-tool filtering, path policy, short-lived identity forwarding, redaction, and HTTP
error mapping. Set an entry's `protocolVersion` to `2025-11-25` to skip the probe for a known
legacy server.

Example:

```dotenv
MINIGENT_MCP_SERVERS=[{"name":"demo","url":"https://example.com/mcp","headers":{"Authorization":"Bearer token"}}]
```

Discovered MCP tools are namespaced as `<server>.<tool>`, for example `demo.echo`.

Identity-aware MCP services can request a fresh user-scoped token on each tool call:

```json
{
  "name": "private-calendar",
  "url": "http://127.0.0.1:8769/mcp",
  "headers": {},
  "forward_identity": true,
  "identity_audience": "private-dav",
  "identity_scopes": ["dav:calendar:read", "dav:calendar:write"]
}
```

Forwarded identity requires these Mindweft process settings:

```text
MINIGENT_MCP_IDENTITY_ISSUER
MINIGENT_MCP_IDENTITY_PRIVATE_KEY
MINIGENT_MCP_IDENTITY_KEY_ID
MINIGENT_MCP_IDENTITY_TOKEN_LIFETIME_SECONDS
```

The private key remains in Mindweft; the MCP service receives only short-lived signed bearer
tokens. Mindweft derives `tenant_id` and `sub` from the authenticated runtime principal rather than
tool arguments. Discovery uses a non-user `__mcp_discovery__` identity. Token lifetime defaults to
300 seconds and cannot exceed 300 seconds. A server cannot combine `forward_identity` with a static
`Authorization` header.

Current scope:
- `initialize`
- `notifications/initialized`
- `tools/list`
- `tools/call`

Because this is a tools-only client, Mindweft sends request/response traffic over POST and does
not open the optional long-lived GET event stream for server-initiated notifications. This keeps
it compatible with the POST-only stdio bridge and shared gateway routes.

The service retains MCP servers that fail discovery, reports them as `unavailable` in
`/config`, and retries them in the background with exponential backoff. When a retry
succeeds, the discovered tools become available to future runs and `/config` reports the
server as `connected`.

## Peer Agent Config

Mindweft can discover configured federated peer agents, proxy task requests to them, and
optionally expose them to the runtime through the `peer_agent_task` tool. Configure peers with
`MINIGENT_PEER_AGENTS`:

```dotenv
MINIGENT_PEER_AGENTS=[{"name":"pi","base_url":"http://127.0.0.1:8010","description":"Local Pi Coding Agent wrapper","capabilities":["repository analysis","codebase inspection"],"side_effects":["runs Pi CLI commands in the allowed workspace"],"version":"0.1.0"}]
# Required only when the agent runtime should be allowed to call peers as a tool:
MINIGENT_ENABLE_PEER_AGENT_TOOL=true
```

The optional `capabilities`, `side_effects`, and `version` fields are included in the
`peer_agent_task` tool description so the model has peer-specific routing context when
deciding whether to delegate.

Discovery endpoints:

- `GET /peer-agents`
- `GET /peer-agents/{name}/agent-card`
- `POST /peer-agents/{name}/tasks`
- `GET /peer-agents/{name}/tasks/{task_id}`
- `POST /peer-agents/{name}/tasks/{task_id}/cancel`
- `GET /peer-agents/{name}/tasks/{task_id}/events`
- `GET /peer-agents/{name}/tasks/{task_id}/artifacts/{artifact_name}`

`GET /peer-agents/{name}/agent-card` fetches the peer's `/agent-card` endpoint and
returns it through Mindweft. Unknown peers return `404`; peer HTTP or JSON failures
return `502`. `POST /peer-agents/{name}/tasks` forwards the request JSON to the peer's
`/tasks` endpoint, and `GET /peer-agents/{name}/tasks/{task_id}` forwards task status
lookups to the peer. The cancel, task events, and artifact endpoints forward to the
peer's matching endpoints; artifact names are limited to `final-output`, `stdout-tail`,
`stderr-tail`, and `events`. These proxy endpoints are for manual federation demos; the
agent runtime does not yet choose or invoke peers automatically.

With the local agent wrapper and Mindweft running, use the root demo script to submit and poll
a peer task through Mindweft:

```bash
uv run python scripts/demo_peer_agent.py
```

Useful overrides:

```bash
MINIGENT_BASE_URL=http://127.0.0.1:8000 \
  uv run python scripts/demo_peer_agent.py \
  --peer pi \
  --cwd /Users/burm/code/minigent \
  --show-events \
  --prompt "Summarize this repository in one paragraph. Do not edit files."
```

To demo cancellation through Mindweft:

```bash
uv run python scripts/demo_peer_agent.py \
  --prompt "Wait 60 seconds, then summarize this repository. Do not edit files." \
  --cancel-after 3
```

The same peer task surface is also available to the agent runtime as the local
`peer_agent_task` tool when `MINIGENT_ENABLE_PEER_AGENT_TOOL=true` and the tool is
allowed by tenant, skill, or capability-profile configuration. The tool requires `peer`,
`cwd`, and `prompt`, accepts optional `poll`, `timeout_seconds`, and
`poll_interval_seconds`, and returns compact task status plus truncated output fields.
This is explicit tool-based delegation only; Mindweft does not automatically choose peer
agents outside normal tool calling.

To demo the runtime tool path with the mock LLM, start Mindweft with
`MINIGENT_ENABLE_PEER_AGENT_TOOL=true` and run:

```bash
uv run python scripts/demo_peer_agent_tool.py
```

That script creates a thread, sends a `/tool peer_agent_task ...` message, runs the
thread, and prints the transcript so you can see the user message, assistant tool call,
tool result, and final assistant reply. It also prints a compact `peer_summary` line with
the peer name, task ID, status, exit code, timeout/cancellation flags, duration, and
short output/error previews before the full transcript.

To run the local agent wrapper, Mindweft, and the runtime tool demo as one local stack:

```bash
./scripts/demo_peer_agent_tool_stack.sh
```

The stack script runs the preflight checker before starting services. You can run it
directly when diagnosing setup:

```bash
uv run python scripts/check_peer_agent_demo.py
```

To check services that are already running instead of checking whether the demo ports are
free:

```bash
uv run python scripts/check_peer_agent_demo.py --check-running
```

The same end-to-end path is available as an opt-in integration test:

```bash
MINIGENT_RUN_PEER_AGENT_INTEGRATION_TESTS=true \
  uv run pytest tests/test_peer_agent_tool_integration.py
```

The legacy `MINIGENT_RUN_INTEGRATION_TESTS=true` flag is still accepted for this test.

The Docker Compose sidecar demo also has an opt-in integration test. It requires Docker
and a usable local OpenCode login because that demo selects the wrapper's OpenCode profile
inside the sidecar:

```bash
MINIGENT_RUN_COMPOSE_INTEGRATION_TESTS=true \
  uv run pytest tests/test_peer_agent_tool_compose_integration.py
```

Pass a custom peer prompt as the first argument:

```bash
./scripts/demo_peer_agent_tool_stack.sh "Summarize the API routes in this repository. Do not edit files."
```

For a local Docker Compose sidecar demo, prepare a minimal OpenCode home and run the
containerized stack:

```bash
./scripts/prepare-opencode-container-home.sh
./scripts/demo_peer_agent_tool_compose.sh
```

Pass a custom containerized peer prompt the same way:

```bash
./scripts/demo_peer_agent_tool_compose.sh "Summarize this repository in one paragraph. Do not edit files."
```

The Compose demo uses [compose.peer-demo.yaml](/Users/burm/code/minigent/compose.peer-demo.yaml).
It exposes Mindweft on `127.0.0.1:8000`, keeps the local agent wrapper internal to the Compose
network in its OpenCode profile, mounts this repository read-only at `/workspace/minigent`, and mounts
`.opencode-container/data` plus `.opencode-container/config` as writable local OpenCode state.
The prepared
`.opencode-container` directory contains copied OpenCode credentials, is ignored by git, and is
made readable by the non-root wrapper container user for this local-only demo. OpenCode may
update files in the mounted data directory while it runs.

The sidecar sets `AGENT_RUNTIME=opencode` and runs `opencode run --format json` inside
the wrapper container. The demo constrains the repository by mounting it read-only and
only giving the wrapper writable access to `.opencode-container` for OpenCode state.
The Compose file sets `AGENT_ARGS_TEMPLATE` with an `OPENCODE_MODEL` override point and
defaults to `openai/gpt-5.2`; set `OPENCODE_MODEL=provider/model` if your OpenCode login
requires a different model.

A Pi backend Compose demo is also available:

```bash
ANTHROPIC_API_KEY=... ./scripts/demo_pi_backend_compose.sh
# or OPENAI_API_KEY=... ./scripts/demo_pi_backend_compose.sh
```

It uses [compose.pi-backend-demo.yaml](/Users/burm/code/minigent/compose.pi-backend-demo.yaml),
builds the wrapper image with Pi installed, enables Mindweft's MCP broker, mounts this
repository read-only at `/workspace/minigent`, and stores Pi state in ignored
`.pi-container/agent`. The demo runs `scripts/demo_pi_mcp_broker.py`, so a successful run
also verifies the brokered `calculator` tool path. By default the script copies host Pi
state from `~/.pi/agent`; set `PI_HOST_AGENT_DIR=/path/to/agent` for a different source,
`PI_CONTAINER_AGENT_DIR=/path/to/repo/.pi-container/agent` for a different target, or pass
`--no-prepare-pi-home` to skip copying and rely on API-key env vars.

Keep the Compose demos running for inspection:

```bash
./scripts/demo_peer_agent_tool_compose.sh --keep-running
./scripts/demo_pi_backend_compose.sh --keep-running
docker compose -f compose.peer-demo.yaml down
docker compose -f compose.pi-backend-demo.yaml down
```

### Stdio MCP Bridge

Many local MCP servers expose the MCP `stdio` transport instead of HTTP. Mindweft includes
a sidecar bridge that exposes one stdio MCP server as a local HTTP MCP endpoint:

```bash
minigent-mcp-stdio-bridge \
  --name filesystem \
  --host 127.0.0.1 \
  --port 8765 \
  -- npx -y @modelcontextprotocol/server-filesystem /tmp
```

Then point Mindweft at the bridge like any other HTTP MCP server:

```dotenv
MINIGENT_MCP_SERVERS=[{"name":"filesystem","url":"http://127.0.0.1:8765/mcp","headers":{}}]
```

The bridge binds to `127.0.0.1` by default and accepts the stdio server command as an
argv array after `--`; it does not run commands through a shell. It buffers stdio MCP
responses up to 16 MiB by default so large single-line JSON tool results such as file reads
can be forwarded; override this with `--stdio-stream-limit <bytes>` if a deployment needs a
different cap. The bridge supports the tools-only MCP scope Mindweft uses today and uses the
official SDK v2 client for subprocess negotiation, stdio request correlation, and protocol result
validation. Mindweft still owns process restart, stream-size limits, tool filtering, and path
policy. For MCP `2026-07-28`, the HTTP side accepts `server/discover`, `tools/list`, and
`tools/call` without requiring an `MCP-Session-Id`; modern requests carry their protocol, client
information, and client capabilities in `params._meta`. Legacy HTTP clients continue to use
`initialize`, `notifications/initialized`, `tools/list`, and `tools/call` with bridge-issued
session IDs. The bridge translates either HTTP-facing form to the protocol negotiated by its
SDK client with the stdio subprocess. Shared compatibility helpers use SDK JSON-RPC models for
responses and remove modern-only result envelope fields when serving legacy HTTP callers.

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

Successful Uvicorn access logs for `GET /health`, `GET /health/live`, and
`GET /health/ready` are suppressed by default so health probes do not flood normal logs. Non-2xx
health responses are still logged. The compatibility endpoint remains available to `minigent
health` clients.

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
uv run pytest
```

## Development

Lint:

```bash
uv run ruff check .
```

Format:

```bash
uv run ruff format .
```

Type check:

```bash
uv run basedpyright
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
uv run python scripts/demo_client.py "hello"
uv run python scripts/demo_client.py "/tool echo hello from tool"
uv run python scripts/demo_client.py "/tool current_time"
```

To continue an existing thread:

```bash
uv run python scripts/demo_client.py --thread-id <thread_id> "follow up"
```

To create a thread with a specific skill:

```bash
uv run python scripts/demo_client.py --skill-name math "/tool echo blocked by skill"
```

To create a thread with multiple prompt-overlay skills plus an explicit capability profile:

```bash
uv run python scripts/demo_client.py \
  --skill-names home-assistant-operator concise \
  --capability-profile home-assistant \
  "turn off the kitchen lights that are still on"
```

## Skills Demo

Skills are execution overlays. They primarily customize the system prompt. Capability profiles
control the effective local-tool and MCP-server surface for a thread.

Users can discover the current tenant's sanitized skill/profile/agent names and descriptions with
`GET /execution-options`, `mindweft options`, or the interactive `/options`, `/skills`, and
`/profiles` chat commands. This discovery surface intentionally omits skill prompts, MCP URLs,
headers, secrets, and raw tool allowlist internals.

Tenant tool config still defines the maximum available tools and MCP servers. A capability profile
can narrow access for a thread, but it cannot expand access beyond the tenant configuration.

For backward compatibility, Mindweft still honors legacy skill-level `allowed_local_tools` and
`mcp_server_names` when a thread selects exactly one such skill and no explicit capability profile
is set. New configs should prefer prompt-only skills plus `capability_profiles`.

The runtime always keeps its built-in tool-use and verification instructions, then appends the
selected skill prompts in order. In other words, skill prompts are overlays, not full replacements
for the runtime prompt, and `POST /threads` does not accept a raw `system_prompt` override.

Clients can use server-side agent presets as named shortcuts for common skill/profile combinations.
For example, `mindweft-client chat` exposes them through `/agent` and creates a new thread with the
preset's configured skills and capability profile.

Use this tenant config with the mock adapter to demo default and explicit skills plus capability
profiles:

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
          "system_prompt":"Answer as a concise support agent."
        },
        {
          "name":"math",
          "system_prompt":"Prefer exact arithmetic over estimation."
        },
        {
          "name":"safe-actions",
          "system_prompt":"Require explicit confirmation before high-risk actions."
        }
      ]
    },
    "capability_profiles":{
      "default_profile":"support",
      "items":[
        {
          "name":"support",
          "allowed_local_tools":["echo","current_time"]
        },
        {
          "name":"math",
          "allowed_local_tools":["calculator"]
        }
      ]
    },
    "agents":{
      "default_agent":"support",
      "items":[
        {
          "name":"support",
          "skill_name":"support",
          "capability_profile":"support",
          "description":"Concise support agent"
        },
        {
          "name":"math",
          "skill_name":"math",
          "capability_profile":"math",
          "description":"Calculator-backed math agent"
        }
      ]
    }
  }
}
```

With the server running:

```bash
uv run python scripts/demo_client.py --tenant-id demo-tenant "/tool echo hello from support"
uv run python scripts/demo_client.py --tenant-id demo-tenant --skill-name math --capability-profile math "/tool echo blocked by profile"
uv run python scripts/demo_client.py --tenant-id demo-tenant --skill-names support safe-actions "hello"
uv run python scripts/demo_client.py --tenant-id demo-tenant --skill-name missing "hello"
```

Expected results:
- Default `support` capability profile allows `echo`, so the reply includes a tool result.
- `math` capability profile narrows tool access, so `/tool echo ...` falls back to a plain mock reply.
- `support` plus `safe-actions` stacks both prompt overlays in order.
- Unknown skills are rejected during thread creation with `400`.

For a Home Assistant deployment, use a prompt-overlay skill with a dedicated capability profile to
append Home Assistant-specific operating guidance while narrowing the thread to the Home Assistant
MCP server:

```dotenv
MINIGENT_TENANT_EXECUTION_CONFIGS={
  "demo-tenant":{
    "llm":{"provider":"openai","model":"gpt-4.1-mini","api_key":"..."},
    "tools":{
      "allowed_local_tools":["current_time"],
      "mcp_servers":[
        {"name":"home-assistant","url":"https://ha.example/api/mcp","headers":{"Authorization":"Bearer ..."}},
        {"name":"docs","url":"https://docs.example/mcp","headers":{"Authorization":"Bearer ..."}}
      ]
    },
    "skills":{
      "items":[
        {
          "name":"home-assistant",
          "description":"Use Home Assistant safely and precisely.",
          "system_prompt":"You are operating against a Home Assistant MCP server. Discover entities before acting, prefer exact entity IDs once resolved, inspect current state before mutation, avoid broad toggles when a specific end state is known, and require explicit user confirmation before security-sensitive actions like unlocking doors, opening garage doors, or disabling alarms."
        },
        {
          "name":"concise",
          "system_prompt":"Respond concisely."
        }
      ]
    },
    "capability_profiles":{
      "items":[
        {
          "name":"home-assistant",
          "allowed_local_tools":["current_time"],
          "mcp_server_names":["home-assistant"]
        }
      ]
    }
  }
}
```

That setup does two things:

- `home-assistant` capability profile narrows MCP access for the thread to `home-assistant`
- `home-assistant` skill appends a Home Assistant-specific prompt overlay with entity-resolution and safety rules

Example:

```bash
uv run python scripts/demo_client.py \
  --tenant-id demo-tenant \
  --skill-names home-assistant concise \
  --capability-profile home-assistant \
  "turn off the kitchen lights that are still on"
```

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
- `retrieve_knowledge`
- `peer_agent_task` when `MINIGENT_ENABLE_PEER_AGENT_TOOL=true`
