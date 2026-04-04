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

## Example flow

```bash
curl -X POST http://127.0.0.1:8000/threads
curl -X POST http://127.0.0.1:8000/threads/<thread_id>/messages \
  -H 'content-type: application/json' \
  -d '{"content":"hello"}'
curl -X POST http://127.0.0.1:8000/threads/<thread_id>/run
```

To force a tool call through the mock adapter:

```bash
curl -X POST http://127.0.0.1:8000/threads/<thread_id>/messages \
  -H 'content-type: application/json' \
  -d '{"content":"/tool echo hello from tool"}'
curl -X POST http://127.0.0.1:8000/threads/<thread_id>/run
```

Local tools currently include:
- `echo`
- `current_time`
- `fetch_url`
- `sleep`
- `calculator`
