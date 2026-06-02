# CLI Reference

Minigent ships two CLI entrypoints that share the same client library:

- **`minigent`** — one-shot commands for scripting and quick checks.
- **`minigent-client`** — interactive chat, voice, and all one-shot commands.

Inside the repo, prefix commands with `uv run`:

```bash
uv run minigent run "hello"
uv run minigent-client chat
```

After installing globally (`uv tool install '.[voice]'`), drop the `uv run` prefix.

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

All one-shot commands work on both `minigent` and `minigent-client`.

### Run a prompt

```bash
minigent run "hello"
echo "summarize this" | minigent run
minigent run --json "hello"
minigent run --stream "hello with progress"
minigent run --stream --show-tool-results "hello"
minigent run --stream --show-reasoning "hello"
minigent run --stream --tokens live "hello"
minigent run --thread <thread-id> "continue"
minigent run --resume-last "continue"
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

### Chat (one-shot)

```bash
minigent chat "hello"
minigent chat --stream "hello with progress"
minigent chat --thread <thread-id> "continue"
minigent chat --resume-last "continue"
```

Same flags as `run`, plus `--print-thread-id` and `--transcript` for printing the full
thread transcript after the reply.

### Threads

```bash
minigent threads                          # list locally remembered threads
minigent threads show <thread-id>         # show a specific thread
minigent threads create                   # create a new thread
minigent threads delete <thread-id>       # delete a thread (requires --admin)
```

### Resume

```bash
minigent resume                           # resume the latest thread (interactive picker in TTY)
minigent resume <thread-id>               # resume a specific thread
```

In a TTY with multiple remembered threads, `resume` without an ID shows an interactive
picker. Use `--no-picker` to skip the picker and resume the latest thread directly.

### Export

```bash
minigent export --format markdown         # export the latest remembered thread
minigent export <thread-id> --format json # export a specific thread
```

### Diagnostics

```bash
minigent ping                             # quick API reachability check
minigent config                           # show local config
minigent config doctor                    # full local/server diagnostic
minigent debug-bundle                     # masked bug-report bundle
minigent debug-bundle --json --output debug-bundle.json
```

`ping` reports API reachability, auth status, MCP broker availability, and model/backend
info. `config doctor` validates base URL, token presence, model, broker URL, and backend
mode. Both support `--json` and return nonzero when a blocking issue is detected.

### Admin commands

Admin commands require `--admin`. Tenant registry commands manage durable tenant lifecycle
state:

```bash
minigent --admin admin tenants list --status active
minigent --admin admin tenants create --id <tenant-id> --slug <slug> --name "Tenant Name" --status active
minigent --admin admin tenants show <tenant-id>
minigent --admin admin tenants update <tenant-id> --plan pro --metadata-json '{"owner":"support"}'
minigent --admin admin tenants suspend <tenant-id>
minigent --admin admin tenants activate <tenant-id>
minigent --admin admin tenants archive <tenant-id>
minigent --admin admin tenants delete <tenant-id>
minigent --admin admin tenants seed --from execution-configs --status active --dry-run
minigent --admin admin tenants seed --from execution-configs --status active
```

Thread and audit admin commands use `--tenant` to select the target tenant:

```bash
minigent --admin admin threads list --tenant <tenant-id>
minigent --admin admin threads show <thread-id> --tenant <tenant-id>
minigent --admin admin threads delete <thread-id> --tenant <tenant-id>
minigent --admin admin threads prune --tenant <tenant-id> --updated-before 2026-05-01T00:00:00Z --dry-run
minigent --admin admin audit list --tenant <tenant-id>
```

## Interactive chat

```bash
minigent-client chat
```

The interactive chat uses `prompt_toolkit` for shell-style editing, persistent local input
history, and multiline input.

### Input behavior

| Key | Default mode | Alt-enter mode |
| --- | --- | --- |
| `Enter` | Submit | Insert newline |
| `Esc+Enter` / `Ctrl+J` | Insert newline | Submit |

Set `MINIGENT_CLIENT_CHAT_SUBMIT_MODE=alt-enter` or pass `--chat-submit-mode alt-enter`
to switch modes. Use `/editor` to compose a long prompt in `$VISUAL` or `$EDITOR`.

### Chat flags

```bash
minigent-client chat                        # plain terminal chat loop
minigent-client chat --stream-runs          # live run/tool/peer progress to stderr
minigent-client chat --resume-last          # resume the last remembered thread
minigent-client chat --once                 # process one prompt then exit
minigent-client chat --show-tool-results    # expanded tool output in streaming mode
minigent-client chat --show-reasoning       # show model reasoning content
minigent-client chat --tokens live          # live token usage display
```

Or set `MINIGENT_CLIENT_STREAM_RUNS=true` for persistent streaming.

### Slash commands

Available during interactive chat:

| Command | Description |
| --- | --- |
| `/help` | Show available commands. |
| `/new` | Create a new thread. |
| `/agent` | List configured agent presets. |
| `/agent current` | Show the current client-side agent label. |
| `/agent <preset>` | Create and switch to a new thread using that preset. |
| `/threads` | List and interactively select a thread (TTY). |
| `/threads <selector>` | Switch to a thread by ID or unique title/date substring. |
| `/switch <id>` | Switch to a specific thread. |
| `/rename <title>` | Rename the current thread. |
| `/copy-id` | Copy the current thread ID. |
| `/cancel` | Cancel the current run. |
| `/compact` | Manually compact thread context. |
| `/export [markdown\|json]` | Export the current thread transcript. |
| `/tokens` | Show estimated current thread size. |
| `/debug` | Toggle debug mode. |
| `/editor` | Open `$EDITOR` for long prompt composition. |
| `/commands` | List saved custom slash commands. |
| `/command set <name> <template>` | Save a custom slash command. Use `{input}` to place invocation text. |
| `/command show <name>` | Show a saved command's template. |
| `/command delete <name>` | Delete a saved command. |
| `/exit`, `/quit` | Exit chat. |

Custom commands are invoked as `/<name> optional input`. Templates without `{input}`
append the invocation text after the template.

### Agent presets

Configure presets with `MINIGENT_CLIENT_AGENT_PRESETS`:

```dotenv
MINIGENT_CLIENT_AGENT_PRESETS={"coding-inspect":{"skill_names":["coding-workspace"],"capability_profile":"inspect"},"home-assistant":{"skill_names":["home-assistant","concise"],"capability_profile":"home-assistant"}}
```

Selecting a preset with `/agent <name>` creates a new thread with that preset's
`skill_name`/`skill_names` and optional `capability_profile`.

## Voice and stdin modes

```bash
minigent-client stdin                       # plain stdin text loop
minigent-client stdin --wake-phrase "hey minigent"
minigent-client stdin --resume-last --once
minigent-client manual-audio                # manual audio capture
minigent-client passive-audio               # passive audio with wake word
minigent-client voice                       # alias for manual-audio
```

The older `--backend chat|stdin|manual-audio|passive-audio` form remains supported:

```bash
minigent-client --backend chat
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

- Auth failures prompt you to check `MINIGENT_API_TOKEN`.
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
| `MINIGENT_BASE_URL` | Default API base URL. |
| `MINIGENT_API_TOKEN` | Default bearer token. |
| `MINIGENT_USER_ID` | Default user ID for trusted-header auth. |
| `MINIGENT_TENANT_ID` | Default tenant ID for trusted-header auth. |
| `MINIGENT_CLIENT_STREAM_RUNS` | Enable streaming by default (`true`/`false`). |
| `MINIGENT_CLIENT_SHOW_TOOL_RESULTS` | Show tool results by default. |
| `MINIGENT_CLIENT_CHAT_SUBMIT_MODE` | `enter` (default) or `alt-enter`. |
| `MINIGENT_CLIENT_AGENT_PRESETS` | JSON object or array of agent presets. |
| `NO_COLOR` | Disable ANSI styling. |
