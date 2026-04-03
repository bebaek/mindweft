# Minigent

Minimal AI agent runtime POC from `DESIGN.md`.

## What it includes

- FastAPI service
- In-memory thread/message store
- Simple agent execution loop
- Pluggable tool registry
- Replaceable LLM adapter boundary
- Deterministic mock adapter for local testing

## Run

```bash
uv venv
source .venv/bin/activate
env UV_CACHE_DIR=.uv-cache uv sync --dev
env UV_CACHE_DIR=.uv-cache uv run uvicorn app.main:app --reload
```

## Test

```bash
env UV_CACHE_DIR=.uv-cache uv run pytest
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
