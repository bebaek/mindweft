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
| `MINIGENT_THREAD_DB_PATH` | Optional SQLite path for persistent thread/message storage plus atomic cross-replica run leases and cancellation. |
| `MINIGENT_OAUTH_STORE_PATH` / `MINIGENT_OAUTH_ENCRYPTION_KEYS` | Shared encrypted SQLite OAuth credentials, login-flow state, and coordinated multi-replica token refresh. |
| `MINIGENT_TENANT_EXECUTION_CONFIGS` | Optional per-tenant LLM, tool, skill, capability, backend, and quality config. |
| `MINIGENT_MCP_BROKER_ENABLED` | Enables the peer-agent MCP broker path when using peer backends. |
| `MINIGENT_MCP_BROKER_DB_PATH` | Optional shared SQLite path for cross-replica MCP broker sessions; bearer tokens are stored only as SHA-256 hashes. |
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
string values under `_meta["io.minigent/private-values"]`. Minigent removes that metadata
before model context, thread history, and run events, and resolves placeholders in
replies sent to the authenticated user. This is a proof of concept rather than a standard MCP
confidential channel: other MCP clients may log or expose `_meta`. Stored messages retain
placeholders, while authenticated message reads rehydrate values that have not expired.
Private values expire after 30 minutes by default and are bounded to 1,000 references per
user/thread scope and 10,000 characters per value; override those limits with
`MINIGENT_PRIVATE_VALUE_TTL_SECONDS`, `MINIGENT_PRIVATE_VALUE_MAX_REFS_PER_THREAD`, and
`MINIGENT_PRIVATE_VALUE_MAX_CHARS`.

The legacy `_meta["io.minigent/carddav-private-values"]` key remains accepted during the DAV
sidecar migration, but new MCP servers should emit the protocol-neutral key. Tools that inspect
raw user text before model use must be explicitly trusted and hidden with the MCP server's
`trusted_input_preprocessor_tools` list; Minigent does not trust server-provided descriptions for
this boundary.

Private values remain in memory by default. To make private values, consent grants, audit
records, and resumable pending tool actions restart-safe, configure encrypted SQLite storage.
The consent tables may share the private-value database file and key; they remain separate
from the thread database:

```bash
PRIVATE_DATA_KEY="$(python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
export MINIGENT_PRIVATE_VALUE_DB_PATH="$PWD/.data/private-data.db"
export MINIGENT_PRIVATE_VALUE_ENCRYPTION_KEY="$PRIVATE_DATA_KEY"
export MINIGENT_PRIVATE_CONSENT_DB_PATH="$PWD/.data/private-data.db"
export MINIGENT_PRIVATE_CONSENT_ENCRYPTION_KEY="$PRIVATE_DATA_KEY"
# Active key versions; see the rotation workflow below before changing them:
export MINIGENT_PRIVATE_VALUE_KEY_VERSION=1
export MINIGENT_PRIVATE_CONSENT_KEY_VERSION=1
```

The SQLite stores encrypt each private value and each consent/action payload independently with
AES-256-GCM and a fresh 96-bit nonce. Private-value tenant/user/thread/reference metadata and
the declared PII kind, plus consent/action tenant/user/thread/consent metadata, are authenticated
as associated data. A known reference resolves only when its placeholder retains that declared
kind; relabeled placeholders remain unresolved for display and fail closed for tool disclosure.
Private values are resolvable only by the user who created or received them, even when another
user in the same tenant can access the thread's placeholder-bearing messages. The database
contains ciphertext, nonces, scoped metadata, statuses, and expiry timestamps but not keys or
plaintext values/tool arguments. Keys must come from the process environment or an external
secret manager; do not commit them or place them beside the database. Startup fails
closed when a database path is configured without a valid corresponding key. Back up keys
separately: losing all copies of a required version makes its existing records unrecoverable.
Consent requests default to a ten-minute TTL and grants to five minutes; override them with
`MINIGENT_PRIVATE_CONSENT_REQUEST_TTL_SECONDS` and
`MINIGENT_PRIVATE_CONSENT_GRANT_TTL_SECONDS`. Redacted disclosure audit records are retained for
30 days by default and bounded to the newest 1,000 records per tenant/user/thread scope in both
memory and SQLite. Configure those bounds with
`MINIGENT_PRIVATE_CONSENT_AUDIT_TTL_SECONDS` and
`MINIGENT_PRIVATE_CONSENT_MAX_AUDIT_RECORDS_PER_SCOPE`. Expired and over-limit records are
pruned during consent activity, audit reads, and encrypted-store startup.

Upgrading a private-value database created before user scoping intentionally drops its
short-lived `private_values` rows on first startup. Those legacy rows cannot be attributed to a
specific user safely; thread messages retain unresolved placeholders until new values are
captured. Consent requests, action records, disclosure audits, and thread history are not removed.

For key rotation, both encrypted stores accept a JSON keyring whose keys are positive version
numbers and whose values are base64-encoded 32-byte keys. New writes use the version selected by
`*_KEY_VERSION`; older versions are decryption-only. To rotate a shared private-data key from
version 1 to version 2:

```bash
OLD_PRIVATE_DATA_KEY='...version-1 key from the secret manager...'
NEW_PRIVATE_DATA_KEY="$(python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
PRIVATE_DATA_KEYS="{\"1\":\"$OLD_PRIVATE_DATA_KEY\",\"2\":\"$NEW_PRIVATE_DATA_KEY\"}"

unset MINIGENT_PRIVATE_VALUE_ENCRYPTION_KEY MINIGENT_PRIVATE_CONSENT_ENCRYPTION_KEY
export MINIGENT_PRIVATE_VALUE_ENCRYPTION_KEYS="$PRIVATE_DATA_KEYS"
export MINIGENT_PRIVATE_CONSENT_ENCRYPTION_KEYS="$PRIVATE_DATA_KEYS"
export MINIGENT_PRIVATE_VALUE_KEY_VERSION=2
export MINIGENT_PRIVATE_CONSENT_KEY_VERSION=2
export MINIGENT_PRIVATE_VALUE_REENCRYPT_ON_STARTUP=true
export MINIGENT_PRIVATE_CONSENT_REENCRYPT_ON_STARTUP=true
```

Start Minigent once with both keys available. On startup, each store transactionally
re-encrypts its surviving rows: private values in one transaction, then consent requests,
pending actions, and disclosure audit records in another. Then stop it, remove version 1 from
both keyrings, disable both `*_REENCRYPT_ON_STARTUP` flags, and restart. A successful restart
with only version 2 verifies that no stored row still requires the retired key. Keep a protected
backup until that verification succeeds. Rotation fails closed and rolls back if any required
old key is missing or any ciphertext fails authentication. The legacy singular
`*_ENCRYPTION_KEY` settings remain supported and represent the active `*_KEY_VERSION`; they can
also be combined with a keyring when their key agrees with its active version.

User-authored message text is locally preprocessed before storage or model use. The default
conservative detector masks email addresses, phone-number-like values, street addresses, and
person names in explicit contexts such as `Email Jane Doe`, `Dr. Jane Doe`, or
`Jane Doe's`. Existing private placeholders are preserved. This regex-based detector is not a
complete PII classifier: it can miss unfamiliar formats, single names without a contextual
cue, non-US-style addresses, and other identifiers, and it can produce false positives.
Set `MINIGENT_INPUT_PII_PROTECTION_ENABLED=false` to disable it explicitly.

Private placeholders are denied at every tool boundary by default. An explicitly trusted MCP
tool can receive selected values only when its server configuration opts into
`resolve_selected` and allowlists the exact JSON argument paths. Resolution happens after the
model creates the tool call and after placeholder-only argument logging, immediately before
the trusted handler runs; the handler receives resolved arguments but never receives the
private-value resolver capability itself. Unapproved paths fail with HTTP 403, and missing or
expired values fail closed. Array paths use `[*]`, for example:

```json
{
  "name": "trusted-mail",
  "url": "http://127.0.0.1:9000/mcp",
  "allowed_tools": ["send"],
  "private_value_policy": "deny",
  "private_value_tool_policies": {
    "send": {
      "mode": "resolve_selected",
      "argument_paths": ["recipient.email", "cc[*].email"],
      "requires_approval": true
    }
  }
}
```

`pass_through` is also available for tools designed to consume opaque placeholders directly;
it never resolves their values. Set `requires_approval` on a per-tool policy to require the same
one-shot approval flow even when a call has no private placeholders, such as a destructive delete.
Tool results are run back through local PII protection before storage, run events, or another
model turn.

A selected disclosure now requires a user-scoped consent grant in addition to administrator
policy. Before requesting consent, Minigent validates every selected placeholder against its
user/thread scope, expiry, reference, and declared PII kind without exposing the plaintext to the
tool layer. Missing, expired, or relabeled placeholders fail with HTTP 409 and create no consent
request, pending action, or disclosure audit record. The first valid attempt creates a redacted
pending request, emits a `private_value.consent_required` run event, and fails the tool call with
HTTP 428. The request contains only the tool name, an argument fingerprint, and PII kinds, counts,
and argument paths. Clients can inspect and decide it through:

```text
GET  /threads/{thread_id}/private-value-consents/pending
POST /threads/{thread_id}/private-value-consents/{consent_id}
POST   /threads/{thread_id}/private-value-consents/{consent_id}/resume
GET    /threads/{thread_id}/private-value-actions
DELETE /threads/{thread_id}/private-value-actions/{consent_id}
GET    /threads/{thread_id}/private-value-disclosures/audit
```

The decision body is `{"approve": true, "one_shot": true}` or `{"approve": false}`. The
`resume` endpoint validates that the private values are still available before atomically claiming
and executing the exact placeholder-bearing tool call that originally requested consent, then
continues the agent loop from its protected result; the model does not need to reconstruct the
call. An expired value therefore leaves the action pending rather than incorrectly marking its
external outcome as uncertain. Claiming is atomic and durable: concurrent or post-restart
attempts to claim the same action fail with HTTP 409 instead of invoking a potentially
side-effecting tool twice. If the process crashes, times out, or loses its connection after the
claim, the action remains in an `executing` state and is not automatically replayed because the
external outcome may be unknown. The claimed record is retained only until its consent grant
expires (five minutes from the decision by default), then removed during consent activity or
encrypted-store startup. `GET /private-value-actions` returns only consent ID, tool name, state,
and expiry—never tool arguments or private values. After reconciling an uncertain external
outcome, clients can `DELETE` the action record; discarding also revokes an unconsumed approval
and appends a redacted `discarded` audit event. Interactive `minigent chat` sessions expose the
`/actions` and `/discard-action <consent-id>`. Reconcile the tool's external state before creating
a replacement action; tools should still support idempotency keys where possible. Grants are
fingerprint of the complete placeholder-bearing argument object, so changing the body or any
other argument requires new consent. Non-one-shot grants remain usable for five minutes; pending
requests expire after ten minutes.
Denials block the identical disclosure until they expire. Consent state is scoped by tenant,
user, and thread. Audit records contain opaque references, paths, and kinds, but never raw
values. The browser displays a confirmation dialog and automatically resumes an approved
action. If that resume has an uncertain outcome, it detects the durable `executing` state,
blocks automatic replay, warns the user to check the external system, and offers to discard the
reconciled action record. Interactive `minigent chat` sessions show the same redacted consent
summary and prompt for a one-shot approval. Consent grants, audit records, and exact
placeholder-bearing pending actions survive restarts when encrypted consent storage is
configured; otherwise they remain in memory.

Private CardDAV and CalDAV server implementations live in the separate
[`private-dav-mcp`](https://github.com/bebaek/private-dav-mcp) project. Minigent retains only the
generic private-value envelope, trusted-preprocessor, selective-disclosure, approval, audit, and
rehydration machinery. Configure DAV servers as ordinary MCP endpoints and explicitly list any
runtime-only input tool under `trusted_input_preprocessor_tools`; keep mutation approval and
selected argument paths in `private_value_tool_policies`. DAV credentials and protocol-specific
environment variables belong on the external sidecar, not the Minigent process.

## Clients

### Browser

The API serves a dependency-free browser client at `/web/`. It uses the streaming run
endpoint to show live LLM, tool, and peer-agent progress, with mobile-friendly run
controls, a stop action, basic assistant markdown rendering, execution option selectors,
image file selection and clipboard paste with previews when server image input is enabled,
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
