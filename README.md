# Minigent

Minigent is a minimal FastAPI agent runtime proof of concept based on
[`DESIGN.md`](DESIGN.md). It provides a small HTTP API for thread-based chat, local and
MCP-backed tools, pluggable LLM adapters, and optional CLI, browser, voice, peer-agent,
and quality-review workflows.

## Highlights

- FastAPI service with thread/message APIs and streaming run events.
- In-memory thread store by default, with optional SQLite persistence.
- Simple agent execution loop with a pluggable local tool registry.
- Replaceable LLM adapter boundary for mock, OpenAI, OpenRouter, OpenAI-compatible, and
  generic OAuth-backed providers.
- Optional MCP tool discovery and invocation over HTTP.
- Optional local/peer-agent backend and peer-agent tool delegation.
- Optional browser, CLI, and voice clients.
- Optional privacy-preserving remote quality critique of sanitized local drafts.

## Quickstart

```bash
uv venv
source .venv/bin/activate
uv sync --dev
uv run uvicorn app.main:app --reload
```

Open the development web client:

```text
http://127.0.0.1:8000/web/
```

Or use the packaged CLI from the repo:

```bash
uv run minigent run "hello"
uv run minigent chat
```

For the reusable coding-workspace runner, copy `.env.coding.template` to `.env.coding`.
The template sets `MINIGENT_THREAD_DB_PATH=.data/minigent-coding-threads.db` so threads
survive restarts. `MINIGENT_CODING_WORKSPACES` can be one path or a comma-separated list of
workspace roots (`MINIGENT_CODING_WORKSPACE` is still accepted for compatibility). Pass
`--no-env-file` to `minigent-coding-workspace` to skip reading `.env.coding` and use only the
process environment plus unified config. Minigent discovers a cwd-local `./minigent.toml`
first, then `$XDG_CONFIG_HOME/minigent/minigent.toml` or
`~/.config/minigent/minigent.toml`. See
[Coding workspace setup](docs/coding-workspace.md) for the MCP-based
workspace tool model, bridge path glob controls, optional trusted-local shell command
support, and optional codebase-memory/code-navigation MCP setup.

## Basic API flow

```bash
curl -X GET 'http://127.0.0.1:8000/threads?limit=20' \
  -H 'Authorization: Bearer dev-token'

curl -X POST http://127.0.0.1:8000/threads \
  -H 'Authorization: Bearer dev-token'

curl -X POST http://127.0.0.1:8000/threads/<thread_id>/messages \
  -H 'Authorization: Bearer dev-token' \
  -H 'content-type: application/json' \
  -d '{"content":"hello"}'

curl -X POST http://127.0.0.1:8000/threads/<thread_id>/run \
  -H 'Authorization: Bearer dev-token'
```

For live progress, use `POST /threads/<thread_id>/run/stream`. It returns newline-delimited
JSON events for run start/completion, LLM requests/responses, tool calls/results, peer-agent
progress, and final assistant messages.

## Configuration at a glance

For desktop/local use, start from [`minigent.toml.template`](minigent.toml.template) or
write a focused starter config with `uv run minigent config init --profile local-coding`
(`basic-chat`, `openrouter`, and `voice` profiles are also available). This file is a
friendly facade for the common app, auth, LLM, coding workspace, MCP, voice, and quality
settings. Unified TOML discovery first checks `./minigent.toml`, then the user-level
`$XDG_CONFIG_HOME/minigent/minigent.toml` or `~/.config/minigent/minigent.toml`; the
coding-workspace runner separately defaults its dotenv file to `./.env.coding` unless
`--no-env-file` is set.
See [`docs/minigent-toml.md`](docs/minigent-toml.md) for the schema, examples, precedence,
and troubleshooting commands. Set `MINIGENT_CONFIG_FILE` to point at a different TOML file.
Existing `.env` files and real environment variables still work; set `MINIGENT_DOTENV_FILE`
to load a dotenv file other than `.env`. A single TOML file can hold multiple named LLM
profiles under `[llm.providers.<name>]`; choose the default with `llm.default` and bind a new
thread with `minigent chat --llm <name>`, `minigent run --llm <name>`, or the browser settings.
Tests or subprocesses that need to ignore cwd-local
default files can set `MINIGENT_CONFIG_DISCOVERY=disabled` while still honoring explicit
config paths. Precedence is `environment > selected .env > minigent.toml > defaults`, so
deployment and secret-management workflows can keep using env overrides. Use
`uv run minigent config print --resolved` to inspect the local resolved env mapping with
secret-looking values masked, or `uv run minigent config export` to generate a best-effort
TOML facade from a running server. `uv run minigent config doctor` also checks the unified
config file, LLM provider prerequisites, coding workspace paths, shell allowlists, and MCP
server shape before probing a running API.

Start from [`.env.template`](.env.template) for full local or deployment settings. Common knobs:

| Setting | Purpose |
| --- | --- |
| `MINIGENT_AUTH_MODE` | Authentication mode: development headers, static tokens, or JWT. |
| `MINIGENT_LLM_PROVIDER` | LLM provider such as `mock`, `openai`, `openrouter`, `openai-compatible`, `generic-oauth`, `google`, or `anthropic`. |
| `MINIGENT_LLM_MODEL` | Model identifier for the selected provider. |
| `MINIGENT_IMAGE_INPUT_ENABLED` | Enables image attachments from CLI/chat clients; unified config key is `[image_input].enabled`. |
| `MINIGENT_THREAD_DB_PATH` | Optional SQLite path for persistent thread/message storage. |
| `MINIGENT_TENANT_EXECUTION_CONFIGS` | Optional per-tenant LLM, tool, skill, capability, backend, and quality config. |
| `MINIGENT_MCP_BROKER_ENABLED` | Enables the peer-agent MCP broker path when using peer backends. |
| `MINIGENT_TOOL_TIMEOUT_SECONDS` | Default wall-clock limit for each runtime tool call before returning a structured timeout error. |
| `MINIGENT_RESPONSES_REASONING_ONLY_RETRIES` | Bounded generic OAuth Responses continuations after reasoning-only output before reporting a retryable provider stall. |

See the [full reference](docs/reference.md) for the complete environment and tenant config
surface.

## Built-in local tools

The local tool registry includes:

- `echo`
- `current_time`
- `fetch_url`
- `sleep`
- `calculator`
- `retrieve_knowledge`, when MiniRAG is configured and allowed
- `peer_agent_task`, when `MINIGENT_ENABLE_PEER_AGENT_TOOL=true` and allowed

Tool availability can be narrowed per tenant, skill, and capability profile. Workspace
capabilities such as filesystem access, editing, shell commands, test runs, builds, and git
operations should be exposed through MCP servers and capability profiles, not as default
Minigent local tools.

Tool results are redacted at the registry boundary before they are streamed to clients,
stored in thread history, or supplied to later LLM turns. The built-in redactor removes
values under sensitive-looking keys and secrets in URL query parameters on a best-effort
basis; it is not a substitute for avoiding unnecessary access to credentials. Tenant and
MCP server configs can override the tool-result redaction policy with `result_redaction`
/ `resultRedaction` (`mode`: `best_effort`, `full`, or `none`).

As an experimental local privacy convention, an MCP tool can return model-safe
`{{pii:kind:reference}}` placeholders in `structuredContent` and place the corresponding
string values under `_meta["io.minigent/carddav-private-values"]`. Minigent removes that
metadata before model context, thread history, and run events, keeps it in a thread-scoped
in-memory store, and resolves placeholders in replies sent to the authenticated user. This
is a proof of concept rather than a standard MCP confidential channel: other MCP clients may
log or expose `_meta`, and values do not survive a restart. Stored messages retain
placeholders, while authenticated message reads rehydrate values that have not expired.
Private values expire after 30 minutes by default and are bounded to 1,000 references per
thread and 10,000 characters per value; override those limits with
`MINIGENT_PRIVATE_VALUE_TTL_SECONDS`, `MINIGENT_PRIVATE_VALUE_MAX_REFS_PER_THREAD`, and
`MINIGENT_PRIVATE_VALUE_MAX_CHARS`.

User-authored message text is locally preprocessed before storage or model use. The default
conservative detector masks email addresses, phone-number-like values, street addresses, and
person names in explicit contexts such as `Email Jane Doe`, `Dr. Jane Doe`, or
`Jane Doe's`. Existing private placeholders are preserved. This regex-based detector is not a
complete PII classifier: it can miss unfamiliar formats, single names without a contextual
cue, non-US-style addresses, and other identifiers, and it can produce false positives.
Set `MINIGENT_INPUT_PII_PROTECTION_ENABLED=false` to disable it explicitly.

Run the private-contacts MCP server with fake contacts for an end-to-end local experiment:

```bash
uv run python scripts/demo_private_contacts_mcp.py
```

It binds to `127.0.0.1:8766` and exposes `http://127.0.0.1:8766/mcp`. Configure that
endpoint as an MCP server named `private-contacts`, allow `contacts_list`, `contacts_get`, and
`contacts_protect_text`, and ask Minigent to list contacts and selected email or phone fields.
`contacts_list` returns
protected names, available field names, and short-lived opaque `contact_ref` values;
`contacts_get` retrieves only requested fields. On message creation, Minigent invokes
`contacts_protect_text` as a trusted preprocessor and masks exact, uniquely matching
address-book contact names before the local detector runs, preserving a usable contact
reference for selective retrieval. Ambiguous address-book names are left to the conservative
local detector rather than being linked to an arbitrary contact. With no CardDAV environment
variables, the server uses intentionally fake data to verify that the model and stored thread
see placeholders while the immediate user reply is rehydrated.

For a read-only Baïkal or other CardDAV address book, export the collection URL and
credentials before starting the same server. Enter the password interactively rather than
putting it in shell history:

```bash
export MINIGENT_CARDDAV_URL='https://baikal.example/dav.php/addressbooks/user/default/'
export MINIGENT_CARDDAV_USERNAME='user'
read -r -s MINIGENT_CARDDAV_PASSWORD
export MINIGENT_CARDDAV_PASSWORD
uv run python scripts/demo_private_contacts_mcp.py
unset MINIGENT_CARDDAV_PASSWORD
```

`MINIGENT_CARDDAV_URL` may identify an address-book collection or its immediate parent. The
server performs a read-only `PROPFIND` to locate the collection, then an `addressbook-query`
`REPORT`; it parses `FN`, `EMAIL`, and `TEL` and returns at most 10 contacts by default (the
tool accepts `limit` up to 50). Basic and Digest authentication are negotiated automatically;
set `MINIGENT_CARDDAV_AUTH_MODE` to `basic` or `digest` only when explicit selection is
needed. TLS verification is enabled; `--insecure-skip-tls-verify` exists only for trusted
local development.

With the CardDAV variables exported, run the sanitized full-path smoke test:

```bash
uv run python scripts/smoke_private_contacts.py
```

It starts the contacts MCP server and a mock-LLM Minigent API, exercises `contacts_list` and
`contacts_get`, verifies that stored/model context contains no raw contact values, verifies
that authenticated history rehydrates those values, and prints counts only. Pass `--fake`
to run the same check against built-in contacts without accessing CardDAV.

## Clients

### Browser

The API serves a dependency-free browser client at `/web/`. It uses the streaming run
endpoint to show live LLM, tool, and peer-agent progress, with mobile-friendly run
controls, a stop action, basic assistant markdown rendering, execution option selectors,
a mobile More menu for secondary actions, a thread context sheet with compaction controls,
a thread drawer backed by `GET /threads`, thread refresh/delete actions, and a collapsible
activity sheet for run details.

#### Mobile UI demo

The `/web/` client is designed to work in a narrow mobile viewport. To demo it on macOS
without a phone, use a browser device emulator:

- Chrome/Brave: open DevTools with `Option+Command+I`, then toggle device mode with
  `Command+Shift+M`.
- Safari: enable developer features, then use `Develop → Enter Responsive Design Mode`
  or `Option+Command+R`.

Use a viewport around `390 × 844` to approximate a modern phone. Send a prompt, then tap
the Activity bar to open the mobile bottom sheet with run events. During a running request,
Send changes to Stop.

To demo on an actual mobile device, bind the development server to your LAN interface.
For the plain API server, pass uvicorn's `--host` option:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
ipconfig getifaddr en0
```

If you start Minigent through the coding-workspace runner, use its API-specific option
instead:

```bash
uv run minigent-coding-workspace --api-host 0.0.0.0 --api-port 8000
ipconfig getifaddr en0
```

Open `http://<mac-lan-ip>:8000/web/` from a phone on the same network, replacing
`<mac-lan-ip>` with the address printed by `ipconfig`. Use a trusted local network only;
the development auth and browser client are not intended to be exposed publicly.

### CLI

Inside the repo:

```bash
uv run minigent run "hello"
uv run minigent chat --stream "hello with progress"
uv run minigent-client chat --resume-last
uv run minigent options
uv run minigent skills
uv run minigent capabilities
uv run minigent threads
uv run minigent resume
uv run minigent export --format markdown
uv run minigent config doctor
```

Install as a reusable CLI app:

```bash
uv tool install '.[voice]'
minigent-client chat
```

See the [CLI reference](docs/cli.md) for all commands, interactive slash commands,
execution option discovery, streaming options, voice modes, and configuration.

### Voice

The optional `voice` extra supports stdin, manual-audio, and passive-audio clients with
speech-to-text and text-to-speech integrations. See the voice/client sections in the
[full reference](docs/reference.md) for provider setup and Linux service helpers.

## Peer agents and MCP

Minigent can delegate work to a local agent wrapper in [`local-agent-wrapper`](local-agent-wrapper).
The wrapper exposes a coding-agent CLI, such as Pi, OpenCode, or Codex, through a small
HTTP task API. Minigent can use that peer either as:

- a `peer_agent_task` tool from the native runtime; or
- the primary `peer_agent` backend for a thread.

When the MCP broker is enabled, Minigent mints short-lived broker sessions so the peer can
call approved Minigent tools without receiving upstream MCP credentials.

## Deployment

The repo includes a production-oriented [`Dockerfile`](Dockerfile) and
[`compose.yaml`](compose.yaml). A typical Compose deployment sets at least:

```dotenv
MINIGENT_AUTH_MODE=jwt
MINIGENT_LLM_PROVIDER=openai
OPENAI_API_KEY=...
MINIGENT_LOG_FORMAT=json
MINIGENT_THREAD_DB_PATH=/data/minigent-threads.db
```

Then run:

```bash
docker compose build
docker compose up -d
```

`compose.yaml` binds to `127.0.0.1:8000` by default so a same-host reverse proxy can expose
it deliberately.

## Documentation

- [CLI reference](docs/cli.md)
- [Coding workspace setup](docs/coding-workspace.md)
- [Dynamic tenant management](docs/dynamic-tenant-management.md)
- [Dynamic user management](docs/dynamic-user-management.md)
- [Layered MCP tool stack](docs/layered-mcp-tool-stack.md)
- [Full reference](docs/reference.md)

## Development

```bash
uv run pytest
MINIGENT_RUN_E2E_TESTS=true uv run pytest -m e2e
uv run ruff check .
uv run ruff format .
uv run basedpyright
```

To use the tracked pre-commit and pre-push hooks, run once per clone:

```bash
./scripts/install-git-hooks.sh
```

The pre-commit hook runs formatting, lint, and type checks. The pre-push hook runs
`MINIGENT_RUN_E2E_TESTS=true uv run pytest` before each push.

### Repository rules

- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages,
  for example:

```text
chore: redact secrets from MCP URL logging
```
