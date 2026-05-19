# Minigent

Minimal AI agent runtime POC from `DESIGN.md`.

## What it includes

- FastAPI service
- In-memory thread/message store by default, with optional SQLite persistence
- Thread context compaction with rolling summary + token-budgeted recent-message tail
  that also drops summarized raw turns from the in-memory transcript to keep memory bounded
- Simple agent execution loop
- Pluggable tool registry
- Replaceable LLM adapter boundary
- OpenAI and OpenRouter support via one OpenAI-compatible adapter
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
- `retrieve_knowledge`: searches tenant-scoped MiniRAG knowledge when configured
- `peer_agent_task`: submits a task to a configured federated peer agent and optionally
  polls for completion when `MINIGENT_ENABLE_PEER_AGENT_TOOL=true`

`fetch_url` is intended for lightweight web context and endpoint checks, not as a full
`curl` replacement. It rejects non-HTTP schemes, private-network hosts, sensitive
request headers such as `authorization` and `cookie`, and responses larger than the
requested `max_bytes` limit are truncated in the returned text/body.

## Run

```bash
uv venv
source .venv/bin/activate
uv sync --dev
uv run uvicorn app.main:app --reload
```

## Dev Web Client

`POST /threads/{thread_id}/run/stream` is also available for clients that want run
progress without waiting for the final JSON response. It returns newline-delimited JSON
with `Content-Type: application/x-ndjson`. The stream emits `run.started`, native runtime
progress such as `llm.request`, `tool.call`, and `tool.result`, peer-backend progress such
as `peer.task.created`/`peer.task.poll`/`peer.task.event`/`peer.task.completed`, then either
`assistant.message` and `run.completed`, or `run.error`. Peer task events are sanitized before
streaming: Minigent forwards event type/status/tool metadata but strips nested message content
from peer agent JSON events. The existing
`POST /threads/{thread_id}/run` endpoint remains unchanged.

The API also serves a small static browser client at `/web` for quick manual testing from
desktop and mobile browsers. It uses the NDJSON run stream to display live run/tool/peer
progress before appending the final assistant reply. The client adjusts to mobile visual
viewport changes so the composer remains usable when the screen keyboard is open. It has
no frontend build step or extra dependencies.

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
another CLI with a custom argv template. Minigent can route tasks to it through the
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
JSONL events when present, falls back to stdout for `final_output`, and captures
stdout/stderr tails separately. With the
wrapper running, use `uv run python scripts/demo_task.py` from `local-agent-wrapper` for
a simple submit-and-poll demo. The demo prints `final_output` and hides the agent's
stderr/progress log unless `--show-log` is passed. Add `--show-events` to print parsed
JSON events. Set `AGENT_RUNTIME=opencode` for the built-in OpenCode profile,
`AGENT_RUNTIME=codex` for the built-in Codex profile, or use
`AGENT_ARGS_TEMPLATE` for another CLI. Task responses include relative `links` and
`artifacts` maps for discovery.

Minigent can also use a configured peer agent as the primary thread execution backend
instead of the built-in LLM/tool loop. Start the wrapper, register it in Minigent, and
select the `peer_agent` backend:

```dotenv
MINIGENT_PEER_AGENTS='[{"name":"pi","base_url":"http://127.0.0.1:8010"}]'
MINIGENT_AGENT_BACKEND=peer_agent
MINIGENT_AGENT_BACKEND_PEER=pi
MINIGENT_AGENT_BACKEND_CWD=/Users/burm/code/minigent
MINIGENT_MCP_BROKER_BASE_URL=http://127.0.0.1:8000
MINIGENT_MCP_BROKER_ENABLED=true
```

With this mode, `POST /threads/{thread_id}/run` sends the Minigent thread context to the
peer agent, polls until the task completes, stores the peer `final_output` as the
assistant message, and returns it as the run reply. Per-tenant execution config can use
the same backend shape:

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

The default backend remains `native`, which preserves the existing Minigent LLM/tool
runtime.

## Remote Quality Enhancement

Minigent can optionally ask a separate remote-quality model to critique a sanitized local
draft before the final assistant message is stored. The main runtime still produces the
initial answer with the tenant's normal LLM and tools. The quality path is advisory only:
Minigent sends a redacted/minimized draft to the quality model, receives critique, then
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
`final_output`. This keeps the default Pi peer profile read-only. When Minigent passes MCP
broker environment variables, the wrapper adds a generated Pi extension that registers
brokered Minigent tools and activates them alongside the read-only file-inspection tools.
Those brokered tools are exposed to Pi with a sanitized `minigent_` prefix. Override with
`AGENT_ARGS_TEMPLATE` when you want persistent Pi sessions, explicit
model/provider flags, write-capable tools, different tool narrowing, or custom Pi
skills/extensions for a specific peer deployment.

Set `MINIGENT_MCP_BROKER_ENABLED=false` or `agent_backend.mcp_broker_enabled=false` if
the peer agent should run without Minigent-brokered MCP tools.

When the peer-agent backend runs, Minigent mints a short-lived MCP broker session for
that thread and passes these environment variables to the wrapper task:

```dotenv
MINIGENT_MCP_BROKER_URL=http://127.0.0.1:8000/mcp/peer/<session>
MINIGENT_MCP_BROKER_TOKEN=<short-lived-token>
MINIGENT_MCP_BROKER_SESSION=<session>
```

The broker exposes the thread's approved Minigent tools through MCP JSON-RPC and
forwards allowed `tools/call` requests through Minigent's existing tool registry, so
OpenCode does not receive upstream MCP server credentials. The wrapper only accepts task
environment variables whose names start with `MINIGENT_MCP_BROKER_` by default; override
that allowlist with `AGENT_ALLOWED_TASK_ENV_PREFIXES` if you add more task-scoped
variables.

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

To start the Pi wrapper, start Minigent in peer-agent backend mode, and run that demo as
one local stack:

```bash
./scripts/demo_pi_backend_stack.sh
```

Pass a custom prompt as the first argument:

```bash
./scripts/demo_pi_backend_stack.sh "Summarize the local-agent-wrapper package. Do not edit files."
```

For an interactive development stack that keeps the Pi wrapper and Minigent running
without launching a demo prompt, use:

```bash
./scripts/dev_pi_peer_stack.sh
```

By default, the wrapper allows tasks only in the Minigent checkout and Minigent sends peer
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
`MINIGENT_PI_WRAPPER_PORT`, `MINIGENT_PI_PEER_NAME`, `AGENT_COMMAND`, and
`AGENT_RUNTIME`.

To smoke-test brokered MCP tool use, run the demo for the configured peer:

```bash
uv run python scripts/demo_opencode_mcp_broker.py
uv run python scripts/demo_pi_mcp_broker.py
```

In the Minigent API logs, look for `mcp_broker.tool_call` to confirm the peer called a
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
and [`compose.yaml`](/Users/burm/code/minigent/compose.yaml) for running Minigent on a remote
host that already manages apps with Docker Compose.

The runtime can persist thread state and message history in SQLite when
`MINIGENT_THREAD_DB_PATH` points at a writable database path. Without that setting,
threads remain in memory and are lost on restart. The optional admin control plane can
also persist tenant execution config in SQLite when `MINIGENT_ADMIN_DB_PATH` points at a
mounted volume.

The current safe deployment shape is a single Minigent container behind your existing
reverse proxy.

Thread history is compacted in memory as conversations grow. Older turns are folded into
the thread summary and removed from the raw message list, so `GET /threads/{thread_id}/messages`
returns the retained recent tail instead of an unbounded full transcript.

Start from [.env.template](/Users/burm/code/minigent/.env.template), then set at least:

```dotenv
MINIGENT_AUTH_MODE=jwt
MINIGENT_LLM_PROVIDER=openai
OPENAI_API_KEY=...
MINIGENT_LOG_FORMAT=json
MINIGENT_THREAD_DB_PATH=/data/minigent-threads.db
```

Bring the service up with:

```bash
docker compose build
docker compose up -d
```

[`compose.yaml`](/Users/burm/code/minigent/compose.yaml) now reads the image name from
`MINIGENT_IMAGE` and falls back to a local `minigent:latest` tag. Set it in your
deployment environment before using a published image:

```dotenv
MINIGENT_IMAGE=ghcr.io/<your-github-user-or-org>/minigent:latest
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
MINIGENT_ENV_FILE=.env.docker docker compose --env-file .env.docker up -d
```

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

You can publish Minigent to a private GHCR package even if this source repo is not hosted
on GitHub. GHCR only needs a GitHub user or organization namespace plus a token that can
write packages.

Log in with a GitHub personal access token that can push packages:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
```

Then publish an image with the helper script in this repo:

```bash
IMAGE_NAMESPACE=<github-user-or-org> \
IMAGE_TAG=latest \
./scripts/docker-build-push.sh
```

Useful overrides:

```bash
IMAGE_NAMESPACE=<github-user-or-org> \
IMAGE_TAG=sha-$(git rev-parse --short HEAD) \
PLATFORMS=linux/amd64,linux/arm64 \
./scripts/docker-build-push.sh
```

The script reads these environment variables:

- `IMAGE_NAMESPACE` (required): GitHub user or organization that owns the package
- `IMAGE_NAME` (default `minigent`): package/image name
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
`ghcr.io/<namespace>/minigent-local-agent-wrapper:<tag>` by default. It supports the same
`REGISTRY`, `IMAGE_NAMESPACE`, `IMAGE_NAME`, `IMAGE_TAG`, and `PLATFORMS` variables, plus
`INSTALL_PI` (default `true`) and `INSTALL_CODEX` (default `false`) build-arg overrides.

For remote deployments, set `MINIGENT_IMAGE` in the deployment env file to the published
tag you want to run, then use `docker compose pull` followed by `docker compose up -d`.

[`compose.yaml`](/Users/burm/code/minigent/compose.yaml) uses whatever auth mode you set
in `.env`; it does not override `MINIGENT_AUTH_MODE`. For local client testing,
`static-tokens` is the easiest path. For remote exposure, prefer `jwt` and include the
required JWT verification settings in `.env`.

By default, [`compose.yaml`](/Users/burm/code/minigent/compose.yaml) binds the API to
`127.0.0.1:8000` so a same-host reverse proxy can publish it safely. If you need direct
network exposure, change the port mapping deliberately instead of binding to all
interfaces by default.

The container exposes `GET /health` for Compose health checks.

[`compose.yaml`](/Users/burm/code/minigent/compose.yaml) mounts a named volume at
`/data`, so `MINIGENT_THREAD_DB_PATH=/data/minigent-threads.db` survives container
restarts.

If you want the optional admin SQLite control plane too, add these settings to `.env`:

```dotenv
MINIGENT_TENANT_CONFIG_SOURCE=store-with-defaults
MINIGENT_ADMIN_DB_PATH=/data/minigent-admin.db
MINIGENT_ADMIN_ENCRYPTION_KEY=replace-with-a-long-random-secret
```

When `MINIGENT_TENANT_CONFIG_SOURCE` is `store` or `store-with-defaults`,
`MINIGENT_ADMIN_ENCRYPTION_KEY` is mandatory.

For the client as a normal CLI app, install the package with the `voice` extra so the
`minigent-client` command is available on your `PATH`:

```bash
uv tool install '.[voice]'
minigent-client stdin --wake-phrase "hey minigent"
```

That installs an isolated tool environment and links the console scripts into uv's tool
bin directory. If the bin directory is not already on your `PATH`, run `uv tool dir
--bin` to find it.

For a remote Linux host that owns the microphone and speaker, use the installer script
over SSH:

```bash
ssh <user>@<host>
cd /path/to/minigent
./scripts/install-client-linux.sh --systemd-user
```

The script installs Linux audio/build prerequisites, installs the package with the
`voice` extra, writes `.env.voice` if it does not already exist, checks ALSA devices,
and can install a `systemd --user` service. Edit `.env.voice` with the Minigent API URL,
voice API token, and STT/TTS provider keys before starting passive audio in production.
The generated env file enables a bell-style wake acknowledgement with
`MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT=bell`; add that setting manually if you already
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
systemctl --user start minigent-client
journalctl --user -u minigent-client -f
```

Use `--enable-linger` with the installer when the user service should continue running
after the SSH user logs out:

```bash
./scripts/install-client-linux.sh --systemd-user --enable-linger
```

If the remote host is only running the Minigent API and does not have the microphone,
run the client on the local machine with audio hardware and point
`MINIGENT_BASE_URL` at the remote API instead.

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

For the simplest client flow against a running server, use the packaged CLI:

```bash
uv run minigent chat "hello"
uv run minigent chat --stream "hello with progress"
uv run minigent chat --thread <thread-id> "continue"
uv run minigent chat --resume-last "continue"
uv run minigent threads show <thread-id>
uv run minigent --admin admin threads list --tenant <tenant-id>
uv run minigent --admin admin threads show <thread-id> --tenant <tenant-id>
uv run minigent --admin admin threads delete <thread-id> --tenant <tenant-id>
```

The repo also exposes the same client entrypoint through `uv run`:

```bash
uv run minigent-client stdin --wake-phrase "hey minigent"
```

By default, assistant replies are printed to the terminal. The examples below use the
installed `minigent-client` command; inside the repo you can replace it with
`uv run minigent-client`.

For a plain terminal chat loop with no wake word, microphone, or spoken output, use the
`chat` subcommand. The older `--backend chat` form remains supported:

```bash
minigent-client chat
```

You can also use `minigent-client stdin`, `minigent-client manual-audio`,
`minigent-client passive-audio`, or `minigent-client voice` (`manual-audio`) instead of
passing `--backend`.

Operational one-shot commands are also available on the shared client entrypoint:

```bash
minigent-client health
minigent-client config
minigent-client threads create
minigent-client threads show THREAD_ID
minigent-client threads delete THREAD_ID
minigent-client --admin admin threads list --tenant TENANT_ID
minigent-client --admin admin threads show THREAD_ID --tenant TENANT_ID
minigent-client --admin admin threads delete THREAD_ID --tenant TENANT_ID
```

Add `--stream-runs`, or set `MINIGENT_CLIENT_STREAM_RUNS=true`, to have `minigent-client`
use `POST /threads/{thread_id}/run/stream` and print live run/tool/peer progress to stderr
before printing or speaking the final assistant reply.

When `chat` runs on an interactive TTY, it uses `prompt_toolkit` for shell-style editing,
persistent local input history, and multiline input. Press `Enter` to submit a message, or
use `Esc+Enter` to insert a newline before submitting. The history file is stored at
`~/.minigent/client-chat-history`. Piped or otherwise non-interactive stdin keeps the
existing plain line-read behavior. In chat mode, pressing Enter on an empty line is
ignored; use `Ctrl-D`, `Ctrl-C`, `/exit`, or `/quit` to exit.

You can also enable local TTS on macOS with:

```bash
MINIGENT_VOICE_TTS_PROVIDER=say
MINIGENT_VOICE_TTS_VOICE=Samantha
minigent-client manual-audio --once
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
minigent-client manual-audio --once
```

For multi-speaker Piper models, also set `MINIGENT_VOICE_TTS_SPEAKER`:

```bash
MINIGENT_VOICE_TTS_PROVIDER=piper
MINIGENT_VOICE_TTS_MODEL=/absolute/path/to/voice.onnx
MINIGENT_VOICE_TTS_SPEAKER=0
minigent-client manual-audio --once
```

`piper-tts` ships as part of the `voice` extra. When
`MINIGENT_VOICE_TTS_MODEL` is a bare voice name like `en_US-lessac-medium`, the client
downloads the `.onnx` and `.onnx.json` files on first use into
`~/.cache/minigent/piper` by default. Override that cache directory with
`MINIGENT_VOICE_TTS_MODEL_DIR` or `--tts-model-dir`. On macOS, Piper synthesis now plays
back through the native `afplay` command so wake-word barge-in does not fight the live
microphone PortAudio stream. On other platforms, Piper playback continues to use
`sounddevice`.

If the Minigent API or upstream LLM returns a transient error during a voice turn, the
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
minigent-client manual-audio --once
```

The client currently supports four backends:

- `chat`: plain terminal chat mode with direct stdin input and terminal replies, with no
  wake-word or audio pipeline
- `stdin`: text-driven wake phrase loop for cheap end-to-end testing
- `manual-audio`: press Enter to activate the microphone, record until silence using
  Silero VAD, transcribe the utterance with OpenAI or OpenRouter speech-to-text, then send the text
  into Minigent and print the assistant reply
- `passive-audio`: continuously listen for a wake word, keep a short pre-roll audio
  buffer, then record until silence and transcribe through the same speech pipeline

`MINIGENT_VOICE_WAKE_PHRASE` is the text trigger for the `stdin` backend. `chat` does
not use wake-word processing at all. In `passive-audio`, the actual wake trigger comes
from the configured wake-word provider: `MINIGENT_VOICE_KEYWORD_PATH` for Porcupine or
`MINIGENT_VOICE_OWW_MODEL` for openWakeWord.

Examples:

```bash
minigent-client chat

minigent-client stdin --wake-phrase "hey minigent"
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
to Minigent; the server treats that as ordinary user content and does not validate or
infer anything on its own.

Example:

```bash
MINIGENT_VOICE_PROMPT_PREAMBLE='timezone=America/Chicago
note=prefer local context' \
minigent-client manual-audio --once
```

For coarse location specifically, `MINIGENT_VOICE_LOCATION` remains available as a
compatibility convenience. When `MINIGENT_VOICE_PROMPT_PREAMBLE` is unset, the client
converts `MINIGENT_VOICE_LOCATION` into client context automatically. If both are set,
`MINIGENT_VOICE_PROMPT_PREAMBLE` wins.

For prompt-level diagnostics, set `MINIGENT_VOICE_DEBUG_SHOW_PROMPT=true`. The client
will print the exact outbound user message after any location prefix is added and before
it sends the request to Minigent.

Manual audio example:

```bash
OPENAI_API_KEY=...
minigent-client manual-audio --once
```

If you want voice input without spoken assistant playback, disable TTS and keep the
assistant reply in the terminal:

```bash
OPENAI_API_KEY=...
MINIGENT_VOICE_TTS_PROVIDER=none
minigent-client manual-audio --once
```

Using OpenRouter for transcription:

```bash
OPENROUTER_API_KEY=...
MINIGENT_VOICE_STT_PROVIDER=openrouter
MINIGENT_VOICE_STT_MODEL=openai/gpt-audio
minigent-client manual-audio --once
```

Using local faster-whisper transcription:

```bash
MINIGENT_VOICE_STT_PROVIDER=faster-whisper
MINIGENT_VOICE_STT_MODEL=base
MINIGENT_VOICE_STT_DEVICE=cpu
MINIGENT_VOICE_STT_COMPUTE_TYPE=int8
MINIGENT_VOICE_STT_LANGUAGE=en
minigent-client manual-audio --once
```

In `manual-audio` mode, press Enter to start recording. The client stops recording after
trailing silence or `MINIGENT_VOICE_MAX_RECORD_SECONDS`, transcribes the utterance, and
then sends the transcript through the normal Minigent thread/run flow.

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
minigent-client passive-audio
```

If you keep the client settings in `.env.voice.docker`, use the wrapper script:

```bash
./scripts/client-docker.sh
```

It exports `.env.voice.docker` into the process environment, then runs:

```bash
minigent-client passive-audio
```

Press `Ctrl-C` to stop the client cleanly. It will print `[idle] shutting down` and
exit without dumping a traceback from the audio backend.

Free `openwakeword` example:

```bash
MINIGENT_VOICE_WAKEWORD_PROVIDER=openwakeword
MINIGENT_VOICE_OWW_MODEL=okay_nabu
minigent-client passive-audio
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
minigent-client passive-audio
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

- `dev-headers`: trust `X-Minigent-*` headers for local development
- `static-tokens`: resolve bearer tokens from `MINIGENT_AUTH_TOKENS`
- `jwt`: verify bearer JWTs and map claims into a `Principal`

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
X-Minigent-User-Id: user-123
X-Minigent-Tenant-Id: tenant-abc
X-Minigent-Admin: false
```

Thread lifecycle endpoints require the auth material for the active mode. Threads are isolated by `tenant_id`, and cross-tenant access returns `404`.

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
  }
}
```

Supported fields:

- `llm.provider`: `mock`, `openai`, `openrouter`, or `openai-compatible`
- `llm.model`, `llm.base_url`, `llm.api_key`, `llm.extra_headers`, `llm.timeout`
- `tools.allowed_local_tools`: local tool allowlist
- `tools.mcp_servers`: per-tenant MCP server definitions
- `skills.default_skill`, `skills.items`: available prompt-overlay skills
- `capability_profiles.default_profile`, `capability_profiles.items`: explicit tool/MCP narrowing profiles

For a developer-oriented example that combines multiple skills with explicit capability profiles,
see the commented block in [.env.template](/Users/burm/code/minigent/.env.template).

The local tool `retrieve_knowledge` is available when Minigent is run with the `minirag`
extra installed and `MINIGENT_MINIRAG_DB_PATH` set to a SQLite database created by
`minirag ingest`.

Recommended setup today:

- `MINIGENT_MINIRAG_BACKEND=dense`
- `MINIGENT_MINIRAG_EMBEDDING_PROVIDER=openrouter`

That matches the current best-performing `minirag` configuration on the FIQA external slices run so far.

Optional retrieval tuning env vars:

- `MINIGENT_MINIRAG_BACKEND`: `lexical`, `dense`, or `hybrid`
- `MINIGENT_MINIRAG_EMBEDDING_PROVIDER`: `hash`, `openai`, or `openrouter`
- `MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT`: optional lexical score weight for `hybrid`
- `MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT`: optional dense score weight for `hybrid`

For local development with `uv`, install it with:

```bash
uv sync --extra minirag
```

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

Because `minirag` is wired in via a local path source during development, rerun
`uv sync --extra minirag` in Minigent after changing the sibling `minirag` repo so the
runtime environment picks up the updated package build.

In `store-with-defaults`, a `*` tenant record in the admin store acts as a default profile before env fallback is considered.

## Admin API

The admin API is an authenticated control plane for tenant execution config and thread inspection. Tenant execution config storage is optional and backed by SQLite.

Enable tenant execution config storage with:

```dotenv
MINIGENT_ADMIN_DB_PATH=.data/minigent-admin.db
MINIGENT_ADMIN_ENCRYPTION_KEY=replace-with-a-long-random-secret
```

Admin endpoints:

- `GET /admin/tenants`
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
X-Minigent-User-Id: admin-user
X-Minigent-Tenant-Id: admin-tenant
X-Minigent-Admin: true
```

Thread inspection endpoints use the active thread store and are tenant-scoped by the `{tenant_id}` path parameter. The list endpoint returns metadata, message counts, and pagination metadata (`limit`, `offset`, `total`, `next_offset`). It accepts `limit`, `offset`, `status`, `profile`, `skill`, `created_after`, and `updated_after` query parameters. The detail endpoint returns metadata, compacted context state, and messages for one thread. Admin deletion removes a thread and its messages and writes an audit record. The prune endpoint deletes matching tenant threads with `updated_at` older than required `updated_before`, with optional `status`, `profile`, and `skill` filters. Add `dry_run=true` to preview `candidate_thread_ids` without deleting threads or writing audit records. The audit endpoint lists deletion/prune records with actor, action, affected count, thread IDs, timestamp, and pagination metadata (`limit`, `offset`, `total`, `next_offset`). It accepts `limit`, `offset`, `action`, `actor`, `created_after`, and `created_before` query parameters. With `MINIGENT_THREAD_DB_PATH` configured, these endpoints can inspect and manage persisted threads and audit records after process restarts.

The packaged CLI can inspect the same thread data when authenticated as an admin:

```bash
minigent --admin admin threads list --tenant TENANT_ID --limit 50
minigent --admin admin threads list --tenant TENANT_ID --status idle --profile default --skill coding
minigent --admin admin threads show THREAD_ID --tenant TENANT_ID
minigent --admin admin threads delete THREAD_ID --tenant TENANT_ID
minigent --admin admin threads prune --tenant TENANT_ID --updated-before 2026-05-01T00:00:00Z
minigent --admin admin threads prune --tenant TENANT_ID --updated-before 2026-05-01T00:00:00Z --dry-run
minigent --admin admin audit list --tenant TENANT_ID --limit 50
minigent --admin admin audit list --tenant TENANT_ID --action threads.prune --actor admin-user --created-after 2026-05-01T00:00:00Z
minigent --api-token ADMIN_TOKEN admin threads list --tenant TENANT_ID --json
```

Secrets such as LLM API keys and MCP headers are accepted on writes but redacted in read responses. If `MINIGENT_TENANT_CONFIG_SOURCE` is `store` or `store-with-defaults`, `MINIGENT_ADMIN_ENCRYPTION_KEY` is required and those secrets are encrypted before being written to SQLite. Updating or deleting a tenant config invalidates the in-process execution cache for that tenant so new runs pick up the change immediately.

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

The service retains MCP servers that fail discovery, reports them as `unavailable` in
`/config`, and retries them in the background with exponential backoff. When a retry
succeeds, the discovered tools become available to future runs and `/config` reports the
server as `connected`.

## Peer Agent Config

Minigent can discover configured federated peer agents, proxy task requests to them, and
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
returns it through Minigent. Unknown peers return `404`; peer HTTP or JSON failures
return `502`. `POST /peer-agents/{name}/tasks` forwards the request JSON to the peer's
`/tasks` endpoint, and `GET /peer-agents/{name}/tasks/{task_id}` forwards task status
lookups to the peer. The cancel, task events, and artifact endpoints forward to the
peer's matching endpoints; artifact names are limited to `final-output`, `stdout-tail`,
`stderr-tail`, and `events`. These proxy endpoints are for manual federation demos; the
agent runtime does not yet choose or invoke peers automatically.

With the local agent wrapper and Minigent running, use the root demo script to submit and poll
a peer task through Minigent:

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

To demo cancellation through Minigent:

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
This is explicit tool-based delegation only; Minigent does not automatically choose peer
agents outside normal tool calling.

To demo the runtime tool path with the mock LLM, start Minigent with
`MINIGENT_ENABLE_PEER_AGENT_TOOL=true` and run:

```bash
uv run python scripts/demo_peer_agent_tool.py
```

That script creates a thread, sends a `/tool peer_agent_task ...` message, runs the
thread, and prints the transcript so you can see the user message, assistant tool call,
tool result, and final assistant reply. It also prints a compact `peer_summary` line with
the peer name, task ID, status, exit code, timeout/cancellation flags, duration, and
short output/error previews before the full transcript.

To run the local agent wrapper, Minigent, and the runtime tool demo as one local stack:

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
MINIGENT_RUN_INTEGRATION_TESTS=true \
  uv run pytest tests/test_peer_agent_tool_integration.py
```

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
It exposes Minigent on `127.0.0.1:8000`, keeps the local agent wrapper internal to the Compose
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
builds the wrapper image with Pi installed, enables Minigent's MCP broker, mounts this
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

Many local MCP servers expose the MCP `stdio` transport instead of HTTP. Minigent includes
a sidecar bridge that exposes one stdio MCP server as a local HTTP MCP endpoint:

```bash
minigent-mcp-stdio-bridge \
  --name filesystem \
  --host 127.0.0.1 \
  --port 8765 \
  -- npx -y @modelcontextprotocol/server-filesystem /tmp
```

Then point Minigent at the bridge like any other HTTP MCP server:

```dotenv
MINIGENT_MCP_SERVERS=[{"name":"filesystem","url":"http://127.0.0.1:8765/mcp","headers":{}}]
```

The bridge binds to `127.0.0.1` by default and accepts the stdio server command as an
argv array after `--`; it does not run commands through a shell. The v1 bridge starts one
stdio MCP server per bridge process and supports the same tools-only MCP scope Minigent
uses today: `initialize`, `notifications/initialized`, `tools/list`, and `tools/call`.

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

Successful Uvicorn access logs for `GET /health` are suppressed by default so Compose
health checks do not flood normal logs. Non-2xx health responses are still logged, and
the endpoint remains available for health probes and `minigent health`.

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

Tenant tool config still defines the maximum available tools and MCP servers. A capability profile
can narrow access for a thread, but it cannot expand access beyond the tenant configuration.

For backward compatibility, Minigent still honors legacy skill-level `allowed_local_tools` and
`mcp_server_names` when a thread selects exactly one such skill and no explicit capability profile
is set. New configs should prefer prompt-only skills plus `capability_profiles`.

The runtime always keeps its built-in tool-use and verification instructions, then appends the
selected skill prompts in order. In other words, skill prompts are overlays, not full replacements
for the runtime prompt, and `POST /threads` does not accept a raw `system_prompt` override.

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
