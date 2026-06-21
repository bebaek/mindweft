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
workspace roots (`MINIGENT_CODING_WORKSPACE` is still accepted for compatibility). See
[Coding workspace setup](docs/coding-workspace.md) for the MCP-based
workspace tool model, bridge path glob controls, optional trusted-local shell command
support, and optional codebase-memory/code-navigation MCP setup.

## Basic API flow

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

For live progress, use `POST /threads/<thread_id>/run/stream`. It returns newline-delimited
JSON events for run start/completion, LLM requests/responses, tool calls/results, peer-agent
progress, and final assistant messages.

## Configuration at a glance

For desktop/local use, start from [`minigent.toml.template`](minigent.toml.template) or
write a focused starter config with `uv run minigent config init --profile local-coding`
(`basic-chat`, `openrouter`, and `voice` profiles are also available). This file is a
friendly facade for the common app, auth, LLM, coding workspace, MCP, voice, and quality
settings. The unified TOML config default is always `./minigent.toml`; the coding-workspace
runner separately defaults its dotenv file to `./.env.coding`.
See [`docs/minigent-toml.md`](docs/minigent-toml.md) for the schema, examples, precedence,
and troubleshooting commands. Set `MINIGENT_CONFIG_FILE` to point at a different TOML file.
Existing `.env` files and real environment variables still work; set `MINIGENT_DOTENV_FILE`
to load a dotenv file other than `.env`. Tests or subprocesses that need to ignore cwd-local
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
| `MINIGENT_LLM_PROVIDER` | LLM provider such as `mock`, `openai`, `openrouter`, `openai-compatible`, `generic-oauth`, or `google`. |
| `MINIGENT_LLM_MODEL` | Model identifier for the selected provider. |
| `MINIGENT_THREAD_DB_PATH` | Optional SQLite path for persistent thread/message storage. |
| `MINIGENT_TENANT_EXECUTION_CONFIGS` | Optional per-tenant LLM, tool, skill, capability, backend, and quality config. |
| `MINIGENT_MCP_BROKER_ENABLED` | Enables the peer-agent MCP broker path when using peer backends. |
| `MINIGENT_TOOL_TIMEOUT_SECONDS` | Default wall-clock limit for each runtime tool call before returning a structured timeout error. |

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

## Clients

### Browser

The API serves a dependency-free development browser client at `/web/`. It uses the
streaming run endpoint to show live LLM, tool, and peer-agent progress.

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
