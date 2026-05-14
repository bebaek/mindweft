# Minigent Local Agent Wrapper

Minimal POC wrapper that exposes a local coding-agent CLI process as a small federated-agent-style HTTP service. It defaults to Pi Coding Agent, but the command profile can be switched to OpenCode, Codex, or a custom argv template.

It proves the local peer-agent boundary:

- submit a prompt to a local coding-agent CLI
- run it in an allowed workspace
- poll task status
- cancel a running task with signals
- capture stdout and stderr tails separately

## Container

Build the default Pi-capable image:

```bash
docker build -t minigent-local-agent-wrapper:latest .
```

From the repository root, build and push the Pi-capable image to GHCR with:

```bash
IMAGE_NAMESPACE=<github-user-or-org> \
IMAGE_TAG=latest \
./scripts/docker-build-push-pi-peer-agent.sh
```

The image installs the `opencode-ai` and Pi Coding Agent npm packages and runs the wrapper as a non-root
`agent` user. To also include the optional Codex CLI in the same image, build with:

```bash
docker build --build-arg INSTALL_CODEX=true -t minigent-local-agent-wrapper:codex .
```

To build an OpenCode-only image without Pi, build with:

```bash
docker build --build-arg INSTALL_PI=false -t minigent-local-agent-wrapper:opencode .
```

For the default `AGENT_RUNTIME=pi`, provide Pi credentials with either API-key environment variables
(such as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) or a mounted Pi config directory. Pi's
config directory can be forced with `PI_CODING_AGENT_DIR=/home/agent/.pi/agent`. From the
repository root, `./scripts/prepare-pi-container-home.sh` copies host `~/.pi/agent` into
ignored `.pi-container/agent` for the Pi Compose demo.

For the repository-level Compose demo, prepare a local OpenCode container home from your
existing local OpenCode login, then run the sidecar stack from the repository root:

```bash
./scripts/prepare-opencode-container-home.sh
./scripts/demo_peer_agent_tool_compose.sh
```

The Compose demo sets `OPENCODE_MODEL=openai/gpt-5.2` by default through
`AGENT_ARGS_TEMPLATE`; override `OPENCODE_MODEL=provider/model` from the shell if your
OpenCode account uses a different model.

## Run

```bash
cd local-agent-wrapper
uv sync --dev
AGENT_ALLOWED_WORKSPACES=/Users/burm/code/minigent \
  uv run uvicorn local_agent_wrapper.app:app --host 127.0.0.1 --port 8010
```

The default Pi invocation is:

```text
pi --mode json --no-session --tools read,grep,find,ls <prompt>
```

The built-in Pi profile starts the process with `cwd` set to the requested allowed workspace.
Override these if needed:

- `AGENT_RUNTIME`: runtime profile, default `pi`; supported built-ins are `pi`, `opencode`, `codex`, and `plain`
- `AGENT_COMMAND`: executable command, default `pi`, `opencode` when `AGENT_RUNTIME=opencode`, or `codex` when `AGENT_RUNTIME=codex`
- `AGENT_ALLOWED_WORKSPACES`: path-list of allowed roots, required for task execution
- `AGENT_ARGS_TEMPLATE`: optional shell-style argv template. Supports `{cwd}` and `{prompt}` placeholders and overrides the built-in runtime argv.
- `AGENT_TAIL_CHARS`: captured stdout/stderr tail size, default `20000`
- `AGENT_EVENT_LIMIT`: parsed JSON event tail size, default `50`
- `AGENT_CANCEL_GRACE_SECONDS`: signal grace period, default `5`
- `CODEX_AGENT_JSON`: set to `false` to disable `codex exec --json`, default `true`
- `CODEX_AGENT_SANDBOX`: Codex sandbox mode, default `read-only`

Codex compatibility:

```bash
AGENT_RUNTIME=codex \
AGENT_ALLOWED_WORKSPACES=/Users/burm/code/minigent \
  uv run uvicorn local_agent_wrapper.app:app --host 127.0.0.1 --port 8010
```

Pi Coding Agent uses Pi's non-interactive JSON event mode by default:

```bash
AGENT_ALLOWED_WORKSPACES=/Users/burm/code/minigent \
  uv run uvicorn local_agent_wrapper.app:app --host 127.0.0.1 --port 8010
```

The built-in Pi profile invokes
`pi --mode json --no-session --tools read,grep,find,ls <prompt>` with the task process
`cwd` set to the requested allowed workspace. This keeps the default Pi peer profile
read-only. When task env includes `MINIGENT_MCP_BROKER_URL` and
`MINIGENT_MCP_BROKER_TOKEN`, the wrapper also passes a generated `--extension` file that
registers brokered Minigent tools with Pi and activates them alongside the read-only
file-inspection tools. Brokered tool names are exposed to Pi with a `minigent_` prefix
and sanitized to provider-compatible characters. The wrapper parses Pi JSONL
`message_end` assistant events for
`final_output`. Use `AGENT_ARGS_TEMPLATE` if you want persistent Pi sessions, a specific
model/provider, write-capable tools, different tool narrowing, or explicit Pi resources
like `--skill` or `--extension`.

Custom CLI example:

```bash
AGENT_COMMAND="my-agent" \
AGENT_ARGS_TEMPLATE="--workspace {cwd} --message {prompt}" \
AGENT_ALLOWED_WORKSPACES=/Users/burm/code/minigent \
  uv run uvicorn local_agent_wrapper.app:app --host 127.0.0.1 --port 8010
```

## API

```text
GET  /health
GET  /agent-card
POST /tasks
GET  /tasks/{task_id}
GET  /tasks/{task_id}/events
GET  /tasks/{task_id}/artifacts/final-output
GET  /tasks/{task_id}/artifacts/stdout-tail
GET  /tasks/{task_id}/artifacts/stderr-tail
GET  /tasks/{task_id}/artifacts/events
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

Task responses include relative `links` and `artifacts` maps so clients can discover the
status, events, cancel, and artifact URLs from the task payload.

Poll parsed JSON events separately:

```bash
curl -s http://127.0.0.1:8010/tasks/<task_id>/events
curl -s 'http://127.0.0.1:8010/tasks/<task_id>/events?after=3'
```

Fetch read-only task artifacts:

```bash
curl -s http://127.0.0.1:8010/tasks/<task_id>/artifacts/final-output
curl -s http://127.0.0.1:8010/tasks/<task_id>/artifacts/stdout-tail
curl -s http://127.0.0.1:8010/tasks/<task_id>/artifacts/stderr-tail
curl -s http://127.0.0.1:8010/tasks/<task_id>/artifacts/events
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

By default the wrapper runs `pi --mode json --no-session --tools read,grep,find,ls`, parses stdout JSONL into
`events_tail` when the CLI emits JSON objects one per line, and exposes the best final
assistant message it can find as `final_output`. The OpenCode profile similarly parses
`opencode run --format json` JSONL events. If no final JSON event is detected and
the process exits successfully, `final_output` falls back to the captured stdout tail. It still keeps
`stdout_tail` as a raw fallback/debug stream. Many agent CLIs write progress, command
transcripts, and other execution logs to stderr; the wrapper captures that stream as
`stderr_tail`, but it is not necessarily error output. Task failure is determined by
`status` and `exit_code`.

The stdout/stderr artifacts are currently bounded tails, not durable full transcripts.
The events artifact returns the parsed in-memory event list for the task.

The demo script prints `final_output` and hides parsed events and the agent stderr log by
default. Add `--show-events` to fetch `/tasks/{task_id}/events` and print parsed events,
or `--show-log` to print the stderr log.

## Test

```bash
uv run pytest
```

Run real CLI integration tests only when the matching local CLI is installed and configured:

```bash
MINIGENT_RUN_OPENCODE_INTEGRATION_TESTS=true uv run pytest tests/test_opencode_integration.py
MINIGENT_RUN_PI_INTEGRATION_TESTS=true uv run pytest tests/test_pi_integration.py
```
