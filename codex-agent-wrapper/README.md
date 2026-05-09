# Minigent Codex Agent

Minimal POC wrapper that exposes a local Codex CLI process as a small federated-agent-style HTTP service.

This is intentionally not wired into Minigent yet. It proves the first boundary:

- submit a prompt to local Codex
- run it in an allowed workspace
- poll task status
- cancel a running task with signals
- capture stdout and stderr tails separately

## Run

```bash
cd codex-agent-wrapper
uv sync --dev
CODEX_AGENT_ALLOWED_WORKSPACES=/Users/burm/code/minigent \
  uv run uvicorn codex_agent_wrapper.app:app --host 127.0.0.1 --port 8010
```

The default Codex invocation is read-only:

```text
codex exec --sandbox read-only --cd <cwd> <prompt>
```

Override these if needed:

- `CODEX_AGENT_COMMAND`: executable command, default `codex`
- `CODEX_AGENT_ALLOWED_WORKSPACES`: path-list of allowed roots, required for task execution
- `CODEX_AGENT_SANDBOX`: Codex sandbox mode, default `read-only`
- `CODEX_AGENT_TAIL_CHARS`: captured stdout/stderr tail size, default `20000`
- `CODEX_AGENT_CANCEL_GRACE_SECONDS`: signal grace period, default `5`

## API

```text
GET  /health
GET  /agent-card
POST /tasks
GET  /tasks/{task_id}
POST /tasks/{task_id}/cancel
```

Example:

```bash
curl -s http://127.0.0.1:8010/tasks \
  -H 'content-type: application/json' \
  -d '{"cwd":"/Users/burm/code/minigent","prompt":"Summarize this repository in one paragraph."}'
```

Then poll:

```bash
curl -s http://127.0.0.1:8010/tasks/<task_id>
```

Or run the scripted demo against the already-running wrapper:

```bash
uv run python scripts/demo_task.py
```

Useful overrides:

```bash
uv run python scripts/demo_task.py \
  --cwd /Users/burm/code/minigent \
  --prompt "List the main runtime components. Do not edit files."
```

## Test

```bash
uv run pytest
```
