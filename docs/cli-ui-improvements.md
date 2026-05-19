# Minigent CLI UI Improvement Ideas

## 1. Better run status display

Show clear phases such as:

- `preparing`
- `sending`
- `streaming`
- `tool running`
- `reviewing`
- `done`

Avoid dumping raw event noise unless `--debug` is enabled.

## 2. Structured tool call output

Render tool calls as compact blocks:

```text
🔧 read(path=README.md) ... done 42ms
```

Add `--show-tool-results` for expanded output.

## 3. Improved streaming layout

Keep assistant text flowing naturally. Put system/run metadata on stderr or behind
`--verbose`, and prevent progress logs from interrupting streamed assistant prose.

## 4. Interactive thread commands

In interactive mode, support commands such as:

```text
/new
/threads
/switch <id>
/rename <title>
/copy-id
/export
/debug
/quit
```

## 5. Thread history picker

Add a simple fuzzy selector for previous threads. Show title, last updated time, and
message count.

## 6. Transcript export

Support transcript export commands:

```bash
minigent export --format markdown
minigent export --format json
```

This is useful for sharing debugging sessions or saving agent work.

## 7. Clearer error messages

Replace raw HTTP/API errors with friendly summaries. For example:

```text
Authentication failed. Check MINIGENT_API_TOKEN.
```

Include `--debug` for full response bodies.

## 8. Config inspection command

Add config inspection and validation commands:

```bash
minigent config show
minigent config doctor
```

Mask secrets by default. Validate base URL, token presence, model, broker URL, and
backend mode.

## 9. Connection test

Add a ping command:

```bash
minigent ping
```

Report API reachability, auth status, MCP broker availability, and model/backend info.

## 10. Better multiline input

In interactive mode:

- Enter sends by default, or make this configurable.
- `Alt+Enter` / `Ctrl+J` inserts a newline.
- `/editor` opens `$EDITOR` for long prompts.

## 11. Shell-friendly non-interactive mode

Make piping clean:

```bash
echo "summarize this" | minigent run
```

Add flags such as:

```bash
--plain
--json
--no-stream
--quiet
```

## 12. Session resume UX

Support session resume commands:

```bash
minigent resume
minigent resume <thread-id>
```

`resume` without an ID should pick the latest thread.

## 13. Debug bundle command

Add a debug bundle command:

```bash
minigent debug-bundle
```

Include thread ID, config minus secrets, recent events, backend mode, MCP status, and
version info.

## 14. CLI visual polish

Use subtle ANSI formatting when a TTY is detected. Disable color automatically for pipes
or with `NO_COLOR`. Clearly distinguish:

- user messages
- assistant responses
- tool calls
- warnings
- errors

## 15. Abort handling

Make `Ctrl+C` graceful:

- First press stops the current generation.
- Second press exits.

Print whether the run was locally aborted or server-cancelled.

## 16. Token count display

Show lightweight token usage in both interactive and one-shot runs:

```text
tokens: prompt 1.8k · completion 420 · total 2.2k · context 18%
```

Suggested UX:

- Default: compact final summary after each run when token metadata is available.
- `--tokens live`: update an inline estimate while streaming, then replace it with final provider usage.
- `--tokens off`: hide token output for scripts or minimal UI.
- `--json`: include structured token usage fields for automation.
- Interactive mode: add `/tokens` to show current thread size, model context window, and estimated remaining budget.

Keep estimates clearly labeled as estimates until provider-reported usage arrives. In debug mode, also show token impact from tool results, summaries/compaction, remote quality review, and peer-agent handoff payloads.
