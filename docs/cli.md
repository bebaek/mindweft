# CLI Reference

Mindweft ships two canonical CLI entrypoints that share the same client library:

- **`mindweft`** — one-shot commands for scripting and quick checks.
- **`mindweft-client`** — interactive chat, voice, and all one-shot commands.

The legacy `minigent` and `minigent-client` names remain available as compatibility aliases.

Inside the repo, prefix commands with `uv run`:

```bash
uv run mindweft run "hello"
uv run mindweft-client chat
```

After installing globally (`uv tool install '.[voice]'`), drop the `uv run` prefix.

## Python API names

New integrations should import `MindweftAPIClient` from `mindweft_client.api_client`,
`MindweftAPIError` from `mindweft_client.errors`, `MindweftClientRuntime` from
`mindweft_client.runtime`, and `MindweftSettings` from `app.settings`.
Legacy import packages and compatibility symbols using the former product prefix remain aliases,
so existing Python integrations do not need an immediate source migration.

## `mindweft-client` config file

Interactive `mindweft-client` can load a TOML config file for stable local defaults. The
lookup order is:

1. `--config <path>`
2. `MINDWEFT_CLIENT_CONFIG` (or legacy `MINIGENT_CLIENT_CONFIG`)
3. `$XDG_CONFIG_HOME/mindweft/client.toml` when `XDG_CONFIG_HOME` is absolute, otherwise
   `~/.config/mindweft/client.toml`
4. `$XDG_CONFIG_HOME/minigent/client.toml` or `~/.config/minigent/client.toml` (legacy)
5. `~/.minigent/client.toml` (legacy)
6. `./.mindweft-client.toml`
7. `./.minigent-client.toml` (legacy)

Mutable client state and prompt history are stored under `$XDG_STATE_HOME/mindweft`, falling
back to `~/.local/state/mindweft`. An existing XDG `minigent` state directory remains in use
until moved explicitly, and files under `~/.minigent` remain available for legacy migration.

Environment variables still override file values, and CLI flags override both. Prefer
keeping secrets such as API tokens and provider keys in environment variables rather than in
this file.

Example:

```toml
base_url = "http://127.0.0.1:8000"
stream_runs = true
show_reasoning = true
chat_submit_mode = "alt-enter"

[principal]
user_id = "demo-user"
tenant_id = "demo-tenant"

[voice]
wake_phrase = "hey mindweft"
stt_provider = "faster-whisper"
stt_device = "cpu"
tts_provider = "say"
tts_voice = "Samantha"
follow_up_timeout_ms = 3000

[voice.wakeword]
provider = "openwakeword"
model = "okay_nabu"
threshold = 0.5

[agents.coding-inspect]
skill_names = ["coding-workspace"]
capability_profile = "inspect"
description = "Read-only coding assistant"
```

## Installation

```bash
# Inside the repo (development)
uv sync --dev

# Global install (reusable CLI app)
uv tool install '.[voice]'
```

Use `--reinstall` when you want uv to recreate the tool environment:

```bash
uv tool install --reinstall '.[voice]'
uv tool install --reinstall --editable '.[voice]'
```

## One-shot commands

All one-shot commands work on both `mindweft` and `mindweft-client`.

### Run a prompt

```bash
mindweft run "hello"
echo "summarize this" | mindweft run
mindweft run --json "hello"
mindweft run --stream "hello with progress"
mindweft run --stream --show-tool-results "hello"
mindweft run --stream --show-reasoning "hello"
mindweft run --stream --tokens live "hello"
mindweft run --thread <thread-id> "continue"
mindweft run --resume-last "continue"
mindweft run --image ./screenshot.png "describe this image"
mindweft run --image before.png --image after.png "compare these"
mindweft run --audio ./meeting.wav "summarize and identify non-speech sounds"
mindweft run --document ./requirements.pdf "review this document"
mindweft run --document ./notes.md "summarize these notes"
```

`run` reads from stdin when no prompt argument is provided. By default the assistant
reply prints to stdout with no extra noise. Useful flags:

| Flag | Effect |
| --- | --- |
| `--json` | Structured JSON output including `reply` and `usage`. |
| `--stream` | Use the NDJSON streaming endpoint; progress prints to stderr. |
| `--no-stream` | Force the non-streaming endpoint (default; explicit for scripts). |
| `--plain` | Plain reply output to stdout (default for text output). |
| `--quiet` | Suppress non-essential stderr progress. |
| `--show-tool-results` | With `--stream`, print expanded tool result bodies to stderr. |
| `--show-reasoning` | Show model reasoning/thinking content when available. |
| `--tokens auto\|live\|off` | Token display mode. `auto` (default) shows a compact final summary; `live` prints provider usage events as they arrive; `off` hides token output. |
| `--thread <id>` | Continue an existing thread. |
| `--resume-last` | Resume the last locally remembered thread. |
| `--skill <name>` | Skill to apply when creating a thread. |
| `--skills <name>...` | Ordered list of prompt-overlay skills. |
| `--capability-profile <name>` | Capability profile to apply. |
| `--llm <name>` | Named LLM profile to bind to a new thread. |
| `--image <path>` | Attach an image file; can be repeated. Requires server-side `[image_input].enabled = true` (or `MINDWEFT_IMAGE_INPUT_ENABLED=true`) and a vision-capable model/provider. |
| `--image-detail auto\|low\|high` | Vision detail hint for attached images. |
| `--audio <path>` | Attach a validated uncompressed PCM WAV file; can be repeated. Requires `[audio_input].enabled = true`, a native supported provider, and an explicit `audio` input capability. |
| `--document <path>` | Attach a PDF or UTF-8 `.txt`, `.md`, `.csv`, or `.log` document; can be repeated. Requires `[document_input].enabled = true`, a native supported provider, and an explicit `document` input capability. |

### Chat (one-shot)

```bash
mindweft chat "hello"
mindweft chat --stream "hello with progress"
mindweft chat --thread <thread-id> "continue"
mindweft chat --resume-last "continue"
mindweft chat --image ./diagram.png "what does this show?"
mindweft chat --audio ./voice-note.wav "analyze this recording"
mindweft chat --document ./requirements.pdf "summarize this"
```

Same flags as `run`, plus `--print-thread-id` and `--transcript` for printing the full
thread transcript after the reply.

### Execution option discovery

List the current tenant's user-visible skills and capability profiles before creating a
thread:

```bash
mindweft options
mindweft skills
mindweft capabilities
mindweft --json options
```

These commands call `GET /execution-options`, which returns sanitized metadata only:
skill/profile names, descriptions, and defaults. It does not expose skill system prompts,
MCP URLs, headers, API keys, or tool allowlist internals.

In interactive chat, use the matching slash commands:

```text
/options
/skills
/profiles
```

Use the reported names when creating threads:

```bash
mindweft threads create --skills coding-workspace concise --capability-profile inspect --llm claude
```

### Threads

```bash
mindweft threads list --search "launch plan" # search active thread titles
mindweft threads search "deployment failure" # search titles and user/assistant messages
mindweft threads search "deployment failure" --scope messages
mindweft threads list --pinned                # list pinned active threads
mindweft threads list --archived              # list archived threads
mindweft threads show <thread-id>              # show a specific thread
mindweft threads create                        # create a new thread
mindweft threads pin <thread-id>               # pin a thread
mindweft threads unpin <thread-id>             # unpin a thread
mindweft threads archive <thread-id>           # archive a thread
mindweft threads restore <thread-id>           # restore an archived thread
mindweft threads delete <thread-id>            # permanently delete a thread
```

### Resume

```bash
mindweft resume                           # resume the latest thread (interactive picker in TTY)
mindweft resume <thread-id>               # resume a specific thread
```

In a TTY with multiple remembered threads, `resume` without an ID shows an interactive
picker. Use `--no-picker` to skip the picker and resume the latest thread directly.

### Export

```bash
mindweft export --format markdown                       # export latest transcript to stdout
mindweft export <thread-id> --format json               # export a transcript as JSON
mindweft export <thread-id> --output transcript.md      # write Markdown to a file
mindweft export <thread-id> --format json --output transcript.json
mindweft export <thread-id> --format archive --output thread.mindweft.json
mindweft import thread.mindweft.json --dry-run          # validate without retaining a thread
mindweft import thread.mindweft.json                    # restore available execution selections
mindweft import thread.mindweft.json --profile-policy strict
mindweft import thread.mindweft.json --organization-policy preserve
mindweft import thread.mindweft.json --timestamp-policy preserve
```

The Markdown and JSON formats produce readable transcripts rather than portable thread archives.
They contain the user-visible messages but omit thread configuration, context summaries, lineage,
and attachment bytes. Transcript messages are rendered for the authenticated user and may include
private values that the server protects internally. Treat transcript files as sensitive and review
them before sharing. The interactive `/export [markdown|json]` command has the same transcript and
privacy semantics and always writes to the interactive output stream.

`--format archive` requests a versioned JSON archive from the server's protected store
representation. Archive files exclude tenant ownership and message creator identities but include
referenced attachment bytes, so treat them as sensitive. Import always creates a new thread for the
authenticated principal, assigns new thread, message, and attachment IDs, and records source message
IDs as provenance.

The current version 4 format supports user, assistant, and tool message history, title, context,
referenced audio, image, and document attachments, source pin/archive organization state, and a
bounded import-provenance chain. Attachment data is base64-encoded in the JSON archive with its MIME
type, byte size, and SHA-256 checksum. Import verifies the manifest,
revalidates attachment content, applies destination attachment capabilities and quotas, and rewrites
message-part references to new attachment IDs. Base64 increases file size, so archive files are
larger than the underlying attachment bytes. Existing version 1 through 3 archives remain importable;
versions 1 and 2 do not contain organization state, and versions 1 through 3 do not carry portable
import provenance.

System messages remain rejected, and fork/compaction lineage is not restored. Organization-state
handling is controlled separately by `--organization-policy`:

| Policy | Behavior |
| --- | --- |
| `reset` | Default. Use destination organization defaults (`pinned=false`, `archived=false`) and warn when recorded source state is not restored. |
| `preserve` | Restore source pin and archive state from a version 3 or 4 archive. For older archives, use destination defaults and print a warning. |

Thread timestamp handling is controlled separately by `--timestamp-policy`:

| Policy | Behavior |
| --- | --- |
| `reset` | Default. Keep fresh destination `created_at` and `updated_at` values after import. |
| `preserve` | Restore source thread `created_at` and `updated_at` values after all imported content is written. Preserved timestamps affect destination sorting, filtering, and retention. |

Execution-selection handling is controlled by `--profile-policy`:

| Policy | Behavior |
| --- | --- |
| `available` | Default. Restore source skills, capability profile, and LLM profile by category; substitute destination defaults and print a warning for categories that cannot resolve. |
| `defaults` | Ignore all recorded source execution selections and use destination defaults, with a summary warning. |
| `strict` | Restore every recorded source selection and reject the import before creating a thread if any selection is unavailable. |

The server API exposes the same behavior through
`POST /threads/import?profile_policy=<policy>&organization_policy=<policy>&timestamp_policy=<policy>`.
Add `dry_run=true` to perform a full import validation without retaining the imported thread. A dry
run exercises the normal entitlement, execution-selection, message, private-value, attachment
content, and storage-quota paths, then removes the temporary thread, attachments, and private-value
state. It returns `thread_id: null`, `dry_run: true`, counts, the selected profile, organization, and
timestamp policies, and the same warnings a real import would return. Attachment dry runs consume the
normal upload rate-limit budget.

Successful non-dry-run imports are idempotent within a tenant by `archive_id`. Repeating an import
with identical normalized archive content and the same profile, organization, and timestamp policies
returns the first response and thread ID without creating another thread or consuming attachment
upload rate-limit budget. The server returns `409 Conflict` if that archive ID is already associated
with changed content or different import policies, or if an identical import is currently in progress.
In-progress claims expire after one hour so interrupted imports can be retried. Deleting the imported
thread removes the associated idempotency record and permits a new import of that archive.

Every successful non-dry-run import records destination-side immediate provenance: the source
`archive_id`, source thread ID, and actual destination import time. Version 4 archive exports embed
this hop plus declared upstream hops in newest-first order. Imports prepend their immediate hop and
retain at most 64 entries; when a full 64-hop archive is imported, the oldest hop is dropped and an
`import_provenance_truncated` warning is returned. Retrieve the immediate hop as `import_provenance`
and the full bounded chain as `import_provenance_chain` from `GET /threads/{thread_id}/lineage`.
Destination import times remain unchanged when `--timestamp-policy preserve` replaces the thread's
own timestamps. Upstream hops are archive-declared provenance and are not independently verified by
the destination.

### Diagnostics

```bash
mindweft ping                             # quick API reachability check
mindweft config                           # show local config
mindweft config doctor                    # full local/server diagnostic
mindweft debug-bundle                     # masked bug-report bundle
mindweft debug-bundle --json --output debug-bundle.json
```

`ping` reports API reachability, auth status, MCP broker availability, and model/backend
info. `config doctor` validates base URL, token presence, model, broker URL, and backend
mode. Both support `--json` and return nonzero when a blocking issue is detected.

### Admin commands

Admin commands require `--admin`. Tenant registry commands manage durable tenant lifecycle
state:

```bash
mindweft --admin admin tenants list --status active
mindweft --admin admin tenants create --id <tenant-id> --slug <slug> --name "Tenant Name" --status active --provisioning-profile generic-v1
mindweft --admin admin tenants show <tenant-id>
mindweft --admin admin tenants update <tenant-id> --plan pro --metadata-json '{"owner":"support"}'
mindweft --admin admin tenants suspend <tenant-id>
mindweft --admin admin tenants activate <tenant-id>
mindweft --admin admin tenants archive <tenant-id>
mindweft --admin admin tenants delete <tenant-id>
mindweft --admin admin tenants seed --from execution-configs --status active --dry-run
mindweft --admin admin tenants seed --from execution-configs --status active
mindweft --admin admin tenants entitlements show <tenant-id>
mindweft --admin admin tenants entitlements set <tenant-id> --features-json '{"mcp":true}' --limits-json '{"max_threads":100}'
mindweft --admin admin tenants entitlements validate <tenant-id> --features-json '{"mcp":true}'
mindweft --admin admin tenants entitlements delete <tenant-id>
```

`--provisioning-profile generic-v1` atomically creates a conservative starter execution
configuration with the tenant: a `general` default agent and skill, a `safe-default`
capability profile, and only the `current_time` and `calculator` local tools. Omit the option
(or pass `none`) to preserve execution-config provisioning as a separate operation.

Thread and audit admin commands use `--tenant` to select the target tenant:

```bash
mindweft --admin admin threads list --tenant <tenant-id>
mindweft --admin admin threads show <thread-id> --tenant <tenant-id>
mindweft --admin admin threads delete <thread-id> --tenant <tenant-id>
mindweft --admin admin threads prune --tenant <tenant-id> --updated-before 2026-05-01T00:00:00Z --dry-run
mindweft --admin admin audit list --tenant <tenant-id>
```

## Interactive chat

```bash
mindweft-client chat
```

The interactive chat uses `prompt_toolkit` for shell-style editing, persistent local input
history, and multiline input.

### Input behavior

| Key | Default mode | Alt-enter mode |
| --- | --- | --- |
| `Enter` | Submit | Insert newline |
| `Esc+Enter` / `Ctrl+J` | Insert newline | Submit |

Set `MINDWEFT_CLIENT_CHAT_SUBMIT_MODE=alt-enter` or pass `--chat-submit-mode alt-enter`
to switch modes. Use `/editor` to compose a long prompt in `$VISUAL` or `$EDITOR`.
Interactive prompts use terminal soft wrapping, so resizing a tmux pane or terminal does not
store pane-width newlines in prompt history.

### Chat flags

```bash
mindweft-client chat                        # plain terminal chat loop
mindweft-client chat --stream-runs          # live run/tool/peer progress to stderr
mindweft-client chat --resume-last          # resume the last remembered thread
mindweft-client chat --once                 # process one prompt then exit
mindweft-client chat --show-tool-results    # expanded tool output in streaming mode
mindweft-client chat --show-reasoning       # show model reasoning content
mindweft-client chat --tokens live          # live token usage display
```

Or set `MINDWEFT_CLIENT_STREAM_RUNS=true` for persistent streaming.

### Slash commands

Available during interactive chat:

| Command | Description |
| --- | --- |
| `/help` | Show available commands. |
| `/new` | Create a new thread, preserving the currently selected agent preset when one is active. |
| `/agent` | List configured agent presets. |
| `/agent current` | Show the current client-side agent label. |
| `/agent <preset>` | Create and switch to a new thread using that preset. |
| `/threads` | List and interactively select a thread (TTY), ordered oldest-to-newest so recent threads remain next to the prompt. |
| `/threads <selector>` | Switch to a thread by ID or unique title/date substring. |
| `/threads search <query>` | Search active server-side titles and user/assistant messages, with bounded matching snippets. |
| `/threads search --messages <query>` | Search only user and assistant message text. |
| `/threads search --titles <query>` | Search only titles. |
| `/threads archived` | List archived server-side threads. |
| `/switch <id>` | Switch to a specific thread. |
| `/rename <title>` | Rename the current thread. |
| `/pin` / `/unpin` | Pin or unpin the current thread. |
| `/archive` / `/restore` | Archive or restore the current thread without affecting related branches. |
| `/copy-id` | Copy the current thread ID. |
| `/cancel` | Cancel the current run. |
| `/fork` | Create and switch to a child branch at the latest visible message. |
| `/fork --pick` or `/fork <number>` | Select a recent visible message and branch from it without handling message IDs. |
| `/lineage` | Show the current thread's available parent, siblings, and direct children by title. |
| `/parent` | Switch to the available parent thread. |
| `/children [number]` | List direct children or switch to a numbered child. |
| `/compact` | Manually compact thread context. |
| `/export [markdown\|json]` | Export the current thread transcript. |
| `/status` | Show the active LLM profile and the last provider-limit snapshot. OpenAI OAuth/Codex windows and reset timing are available to tenant owners/admins, or to members when the tenant enables `llm_provider_status`; the command does not make a provider request. |
| `/tokens` | Show estimated current thread size. |
| `/debug` | Toggle debug mode. |
| `/editor` | Open `$EDITOR` for long prompt composition. |
| `/image <path...>` | Queue one or more image files for the next message. Requires server-side `[image_input].enabled = true` (or `MINDWEFT_IMAGE_INPUT_ENABLED=true`) and a vision-capable model/provider. |
| `/image paste`, `/image clipboard` | On macOS, use `pngpaste` to queue a PNG image from the system clipboard for the next message. |
| `/image list` | Show images queued for the next message. |
| `/image clear` | Clear queued images without sending them. |
| `/document <path...>` | Queue one or more PDF or UTF-8 `.txt`, `.md`, `.csv`, or `.log` files for the next message. The selected profile must explicitly support document input. |
| `/document list` | Show documents queued for the next message. |
| `/document clear` | Clear queued documents without sending them. |
| `/commands` | List saved custom slash commands. |
| `/command set <name> <template>` | Save a custom slash command. Use `{input}` to place invocation text. |
| `/command show <name>` | Show a saved command's template. |
| `/command delete <name>` | Delete a saved command. |
| `/exit`, `/quit` | Exit chat. |

Custom commands are invoked as `/<name> optional input`. Templates without `{input}`
append the invocation text after the template.

### Agent presets

Configure presets with `MINDWEFT_CLIENT_AGENT_PRESETS`:

```dotenv
MINDWEFT_CLIENT_AGENT_PRESETS={"coding-inspect":{"skill_names":["coding-workspace"],"capability_profile":"inspect"},"home-assistant":{"skill_names":["home-assistant","concise"],"capability_profile":"home-assistant"}}
```

Selecting a preset with `/agent <name>` creates a new thread with that preset's
`skill_name`/`skill_names` and optional `capability_profile`. Subsequent `/new` commands
inherit the selected preset so you can start a fresh thread without re-selecting the same
agent.

## Voice and stdin modes

```bash
mindweft-client stdin                       # plain stdin text loop
mindweft-client stdin --wake-phrase "hey mindweft"
mindweft-client stdin --resume-last --once
mindweft-client manual-audio                # manual audio capture
mindweft-client passive-audio               # passive audio with wake word
mindweft-client voice                       # alias for manual-audio
```

The older `--backend chat|stdin|manual-audio|passive-audio` form remains supported:

```bash
mindweft-client --backend chat
```

Voice modes require the `voice` extra:

```bash
uv tool install '.[voice]'
```

## Streaming output

With `--stream` (one-shot) or `--stream-runs` (interactive), the CLI uses
`POST /threads/{thread_id}/run/stream` and prints live progress to stderr:

```text
● preparing
● sending
🔧 echo(text=hello) ... done 42ms
● done
```

Assistant replies print to stdout. Tool call lines show tool name, arguments, and
duration. Use `--show-tool-results` to include indented tool result bodies. Use
`--verbose` with one-shot streaming commands to include extra progress metadata such as
LLM iteration numbers.

Token display defaults to compact final thread-context size estimates plus provider usage
when available. Use `--tokens live` to print provider usage events as they arrive, or
`--tokens off` to hide token summaries. Streaming `--json` output includes a structured
`usage` object when the API reports provider token metadata.

## Thread resume

The CLI remembers the most recent thread per server and principal. After a successful
user turn, the thread ID is saved locally. Later commands with `--resume-last` continue
from that thread.

Interactive chat also seeds prompt history from the thread's server-side user message
metadata so history navigation contains only prompts, even when model-facing messages
include client context.

## Error handling

The CLI reports common API failures with short friendly errors on stderr:

- Auth failures prompt you to check `MINDWEFT_API_TOKEN`.
- Connection failures suggest checking `--base-url` and whether the server is running.
- Server/timeout/malformed-response failures are categorized without dumping raw HTTP
  bodies by default.

Technical detail is available with `--verbose`. In `--json` mode, errors are emitted as
a structured `{"error": ...}` object and the process exits nonzero.

## Abort handling

Press `Ctrl+C` during an interactive chat run to abort the current turn and return to the
prompt. Press `Ctrl+C` again at the prompt to exit. One-shot commands return exit code 130
on abort.

Streaming runs request cancellation from the API and close the stream; the API resets the
thread to idle once cancellation is handled. Non-streaming runs report that server
cancellation is unavailable.

## Visual styling

When stderr/stdout is connected to a TTY, the CLI uses subtle ANSI styling for interactive
prompts, assistant replies, progress, tool calls, peer events, warnings, and errors. Color
is disabled automatically for pipes and can be disabled with `NO_COLOR=1`.

## Global flags

These flags work across all commands:

| Flag | Description |
| --- | --- |
| `--base-url <url>` | API base URL (default: `http://127.0.0.1:8000`). |
| `--api-token <token>` | Bearer token for Authorization header. |
| `--user-id <id>` | Trusted-header user ID (default: `demo-user`). |
| `--tenant-id <id>` | Trusted-header tenant ID (default: `demo-tenant`). |
| `--admin` | Mark the principal as an admin. |
| `--trace` | Send a W3C traceparent header and print the trace ID. |
| `--json` | Structured JSON output. |
| `--verbose` | Print extra metadata. |

## Environment variables

| Variable | Description |
| --- | --- |
| `MINDWEFT_BASE_URL` | Default API base URL. |
| `MINDWEFT_API_TOKEN` | Default bearer token. |
| `MINDWEFT_USER_ID` | Default user ID for trusted-header auth. |
| `MINDWEFT_TENANT_ID` | Default tenant ID for trusted-header auth. |
| `MINDWEFT_CLIENT_STREAM_RUNS` | Enable streaming by default (`true`/`false`). |
| `MINDWEFT_CLIENT_SHOW_TOOL_RESULTS` | Show tool results by default. |
| `MINDWEFT_CLIENT_CHAT_SUBMIT_MODE` | `enter` (default) or `alt-enter`. |
| `MINDWEFT_CLIENT_AGENT_PRESETS` | JSON object or array of client-side agent presets. Presets reference tenant-defined skills/capability profiles discoverable with `mindweft options`. |
| `NO_COLOR` | Disable ANSI styling. |
