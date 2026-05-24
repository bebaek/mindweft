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
survive restarts. See [Coding workspace setup](docs/coding-workspace.md) for the MCP-based
workspace tool model.

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

Start from [`.env.template`](.env.template) for local or deployment settings. Common knobs:

| Setting | Purpose |
| --- | --- |
| `MINIGENT_AUTH_MODE` | Authentication mode: development headers, static tokens, or JWT. |
| `MINIGENT_LLM_PROVIDER` | LLM provider such as `mock`, `openai`, `openrouter`, `openai-compatible`, or `generic-oauth`. |
| `MINIGENT_LLM_MODEL` | Model identifier for the selected provider. |
| `MINIGENT_THREAD_DB_PATH` | Optional SQLite path for persistent thread/message storage. |
| `MINIGENT_TENANT_EXECUTION_CONFIGS` | Optional per-tenant LLM, tool, skill, capability, backend, and quality config. |
| `MINIGENT_MCP_BROKER_ENABLED` | Enables the peer-agent MCP broker path when using peer backends. |

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

## Clients

### Browser

The API serves a dependency-free development browser client at `/web/`. It uses the
streaming run endpoint to show live LLM, tool, and peer-agent progress.

### CLI

Inside the repo:

```bash
uv run minigent run "hello"
uv run minigent chat --stream "hello with progress"
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

The previous long README now lives in [`docs/reference.md`](docs/reference.md). Additional
focused docs and planning notes are available in:

- [Coding workspace setup](docs/coding-workspace.md)
- [CLI UI improvements](docs/cli-ui-improvements.md)
- [CLI unification plan](docs/cli-unification-plan.md)

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
uv run basedpyright
```

Use Conventional Commits for commit messages, for example:

```text
chore: redact secrets from MCP URL logging
```
