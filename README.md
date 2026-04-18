# Minigent

Minimal AI agent runtime POC from `DESIGN.md`.

## What it includes

- FastAPI service
- In-memory thread/message store
- In-memory thread context compaction with rolling summary + token-budgeted recent-message tail
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
uv sync --dev
uv run uvicorn app.main:app --reload
```

## Docker Compose Deployment

This repo now includes a production-oriented [`Dockerfile`](/Users/burm/code/minigent/Dockerfile)
and [`compose.yaml`](/Users/burm/code/minigent/compose.yaml) for running Minigent on a remote
host that already manages apps with Docker Compose.

The current runtime has two important persistence boundaries:

- Thread state and message history are stored in memory, so restarting the container loses
  active threads.
- The optional admin control plane can persist tenant execution config in SQLite when
  `MINIGENT_ADMIN_DB_PATH` points at a mounted volume.

That means the current safe deployment shape is a single Minigent container behind your
existing reverse proxy.

Start from [.env.example](/Users/burm/code/minigent/.env.example), then set at least:

```dotenv
MINIGENT_AUTH_MODE=jwt
MINIGENT_LLM_PROVIDER=openai
OPENAI_API_KEY=...
MINIGENT_LOG_FORMAT=json
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

For remote deployments, set `MINIGENT_IMAGE` in the deployment env file to the published
tag you want to run, then use `docker compose pull` followed by `docker compose up -d`.

[`compose.yaml`](/Users/burm/code/minigent/compose.yaml) uses whatever auth mode you set
in `.env`; it does not override `MINIGENT_AUTH_MODE`. For local voice-daemon testing,
`static-tokens` is the easiest path. For remote exposure, prefer `jwt` and include the
required JWT verification settings in `.env`.

By default, [`compose.yaml`](/Users/burm/code/minigent/compose.yaml) binds the API to
`127.0.0.1:8000` so a same-host reverse proxy can publish it safely. If you need direct
network exposure, change the port mapping deliberately instead of binding to all
interfaces by default.

The container exposes `GET /health` for Compose health checks.

If you want the optional admin SQLite control plane, add these settings to `.env` and
mount `/data` in Compose:

```dotenv
MINIGENT_TENANT_CONFIG_SOURCE=store-with-defaults
MINIGENT_ADMIN_DB_PATH=/data/minigent-admin.db
MINIGENT_ADMIN_ENCRYPTION_KEY=replace-with-a-long-random-secret
```

When `MINIGENT_TENANT_CONFIG_SOURCE` is `store` or `store-with-defaults`,
`MINIGENT_ADMIN_ENCRYPTION_KEY` is mandatory.

For the voice daemon as a normal CLI app, install the package with the `voice` extra so
the `minigent-voice-daemon` command is available on your `PATH`:

```bash
uv tool install '.[voice]'
minigent-voice-daemon --wake-phrase "hey minigent"
```

That installs an isolated tool environment and links the console scripts into uv's tool
bin directory. If the bin directory is not already on your `PATH`, run `uv tool dir
--bin` to find it.

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
uv run minigent chat --thread <thread-id> "continue"
uv run minigent chat --resume-last "continue"
uv run minigent threads show <thread-id>
```

The repo also exposes the same voice-daemon entrypoint through `uv run`:

```bash
uv run minigent-voice-daemon --wake-phrase "hey minigent"
```

By default, assistant replies are printed to the terminal. The examples below use the
installed `minigent-voice-daemon` command; inside the repo you can replace it with
`uv run minigent-voice-daemon`. You can also enable local TTS on macOS with:

```bash
MINIGENT_VOICE_TTS_PROVIDER=say
MINIGENT_VOICE_TTS_VOICE=Samantha
minigent-voice-daemon --backend manual-audio --once
```

With `MINIGENT_VOICE_TTS_PROVIDER=say`, passive mode also supports wake-word barge-in:
saying the wake word again while the assistant is speaking will stop `say` and switch
back to listening.

When local TTS is enabled, the daemon strips common Markdown formatting such as `*`, `` ` ``,
headers, lists, and Markdown links before feeding text to the speech engine, while still
printing the original assistant reply to the terminal. Structural Markdown like headers
and list items is converted into short sentence boundaries so TTS does not run them into
surrounding text.

For higher-quality local TTS on macOS or Linux, install the voice extra and configure
Piper with a model path or model name:

```bash
MINIGENT_VOICE_TTS_PROVIDER=piper
MINIGENT_VOICE_TTS_MODEL=en_US-lessac-medium
minigent-voice-daemon --backend manual-audio --once
```

For multi-speaker Piper models, also set `MINIGENT_VOICE_TTS_SPEAKER`:

```bash
MINIGENT_VOICE_TTS_PROVIDER=piper
MINIGENT_VOICE_TTS_MODEL=/absolute/path/to/voice.onnx
MINIGENT_VOICE_TTS_SPEAKER=0
minigent-voice-daemon --backend manual-audio --once
```

`piper-tts` ships as part of the `voice` extra. When
`MINIGENT_VOICE_TTS_MODEL` is a bare voice name like `en_US-lessac-medium`, the daemon
downloads the `.onnx` and `.onnx.json` files on first use into
`~/.cache/minigent/piper` by default. Override that cache directory with
`MINIGENT_VOICE_TTS_MODEL_DIR` or `--tts-model-dir`. Piper playback uses the same local
PortAudio stack as microphone capture through `sounddevice`, so you do not need a
separate `afplay`/`aplay` integration in the daemon.

The daemon currently supports three backends:

- `stdin`: text-driven wake phrase loop for cheap end-to-end testing
- `manual-audio`: press Enter to activate the microphone, record until silence using
  Silero VAD, transcribe the utterance with OpenAI or OpenRouter speech-to-text, then send the text
  into Minigent and print the assistant reply
- `passive-audio`: continuously listen for a wake word, keep a short pre-roll audio
  buffer, then record until silence and transcribe through the same speech pipeline

`MINIGENT_VOICE_WAKE_PHRASE` is the text trigger for the `stdin` backend. In
`passive-audio`, the actual wake trigger comes from the configured wake-word provider:
`MINIGENT_VOICE_KEYWORD_PATH` for Porcupine or `MINIGENT_VOICE_OWW_MODEL` for
openWakeWord.

Examples:

```bash
minigent-voice-daemon --wake-phrase "hey minigent"
# ignored
hello there
# activates and uses the rest of the line as the utterance
hey minigent summarize the latest thread
# or activate first, then provide the utterance on the next line
hey minigent
show me the transcript
```

Manual audio example:

```bash
OPENAI_API_KEY=...
minigent-voice-daemon --backend manual-audio --once
```

Using OpenRouter for transcription:

```bash
OPENROUTER_API_KEY=...
MINIGENT_VOICE_STT_PROVIDER=openrouter
MINIGENT_VOICE_STT_MODEL=openai/gpt-audio
minigent-voice-daemon --backend manual-audio --once
```

Using local faster-whisper transcription:

```bash
MINIGENT_VOICE_STT_PROVIDER=faster-whisper
MINIGENT_VOICE_STT_MODEL=base
MINIGENT_VOICE_STT_DEVICE=cpu
MINIGENT_VOICE_STT_COMPUTE_TYPE=int8
MINIGENT_VOICE_STT_LANGUAGE=en
minigent-voice-daemon --backend manual-audio --once
```

In `manual-audio` mode, press Enter to start recording. The daemon stops recording after
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
minigent-voice-daemon --backend passive-audio
```

If you keep the daemon settings in `.env.voice.docker`, use the wrapper script:

```bash
./scripts/voice-daemon-docker.sh
```

It exports `.env.voice.docker` into the process environment, then runs:

```bash
minigent-voice-daemon --backend passive-audio
```

Press `Ctrl-C` to stop the daemon cleanly. It will print `[idle] shutting down` and
exit without dumping a traceback from the audio backend.

Free `openwakeword` example:

```bash
MINIGENT_VOICE_WAKEWORD_PROVIDER=openwakeword
MINIGENT_VOICE_OWW_MODEL=okay_nabu
minigent-voice-daemon --backend passive-audio
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

If no speech arrives within `MINIGENT_VOICE_POST_WAKE_SPEECH_TIMEOUT_MS` after the wake
word, passive mode ignores that activation and returns to idle without sending audio to
STT.

To make short back-and-forth follow-ups feel more natural, set
`MINIGENT_VOICE_FOLLOW_UP_TIMEOUT_MS` to keep listening briefly after the assistant
finishes speaking. During that window, `passive-audio` accepts one follow-up utterance
without requiring the wake word, then returns to normal wake-word mode after silence.

If you need to inspect captured audio, set `MINIGENT_VOICE_DEBUG_CAPTURE_PATH` or pass
`--debug-capture-path`. The daemon will print capture metadata and write the last WAV
capture there before transcription. That is useful for comparing `manual-audio` and
`passive-audio` artifacts.

If you need to inspect the OpenRouter STT request/response payloads, set
`MINIGENT_VOICE_STT_DEBUG_PATH` or pass `--stt-debug-path`. The daemon and replay tool
will write debug artifacts such as `request.json` and `response.json` there.

When STT returns a bad assistant-style answer instead of a transcript, the daemon now
logs the failure, ignores that capture, and returns to idle instead of crashing.

If you want to experiment with audio level differences before STT, the replay tool also
supports `--gain`, `--normalize-peak`, `--pad-leading-ms`, and `--pad-trailing-ms`. That
is useful when comparing quieter passive captures against louder manual captures, or when
you want to approximate the passive daemon's STT padding against a saved WAV.

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
- `MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT`
- `MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT_SOUND`
- `MINIGENT_VOICE_STT_PROVIDER`
- `MINIGENT_VOICE_STT_DEVICE`
- `MINIGENT_VOICE_STT_COMPUTE_TYPE`
- `MINIGENT_VOICE_STT_LANGUAGE`
- `MINIGENT_VOICE_TTS_PROVIDER`
- `MINIGENT_VOICE_TTS_VOICE`
- `MINIGENT_VOICE_TTS_MODEL`
- `MINIGENT_VOICE_TTS_MODEL_DIR`
- `MINIGENT_VOICE_TTS_SPEAKER`
- `MINIGENT_VOICE_WAKEWORD_PROVIDER`
- `MINIGENT_VOICE_SKILL`
- `MINIGENT_VOICE_THREAD_ID`
- `MINIGENT_VOICE_AUDIO_DEVICE`
- `MINIGENT_VOICE_DEBUG_CAPTURE_PATH`
- `MINIGENT_VOICE_STT_DEBUG_PATH`
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

You can put provider settings in a local `.env` file. Start from [.env.example](/Users/burm/code/minigent/.env.example).

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

The admin API is an optional control plane for tenant execution config backed by SQLite.

Enable it with:

```dotenv
MINIGENT_ADMIN_DB_PATH=.data/minigent-admin.db
MINIGENT_ADMIN_ENCRYPTION_KEY=replace-with-a-long-random-secret
```

Admin endpoints:

- `GET /admin/tenants`
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

The service skips MCP servers that fail during startup and exposes connected servers in `/config`.

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

## Skills Demo

Skills are execution overlays, not independent permission grants. Tenant tool config defines the
maximum available tools and MCP servers. When a thread activates a skill, the runtime narrows the
effective tool and MCP access to the intersection of the tenant configuration and the skill
allowlists. Skills can reduce access for a thread, but they cannot expand access beyond the tenant
configuration.

Use this tenant config with the mock adapter to demo default and explicit skills:

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
          "system_prompt":"Answer as a concise support agent.",
          "allowed_local_tools":["echo","current_time"]
        },
        {
          "name":"math",
          "system_prompt":"Prefer exact arithmetic over estimation.",
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
uv run python scripts/demo_client.py --tenant-id demo-tenant --skill-name math "/tool echo blocked by skill"
uv run python scripts/demo_client.py --tenant-id demo-tenant --skill-name missing "hello"
```

Expected results:
- Default `support` skill allows `echo`, so the reply includes a tool result.
- `math` narrows tool access, so `/tool echo ...` falls back to a plain mock reply.
- Unknown skills are rejected during thread creation with `400`.

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
