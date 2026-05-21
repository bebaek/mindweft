# Minigent CLI UI Improvement Ideas

Status legend: `[x]` done, `[~]` partially done, `[ ]` not done.

## [x] 1. Better run status display

Implemented by `0fed3f4 feat(cli): improve streaming status output`.

Show clear phases such as:

- `preparing`
- `sending`
- `streaming`
- `tool running`
- `reviewing`
- `done`

Avoid dumping raw event noise unless `--debug` is enabled.

## [x] 2. Structured tool call output

Implemented by compact streaming tool-call status lines plus opt-in expanded tool result output with `--show-tool-results`, including forwarded peer-agent tool details when available.

Render tool calls as compact blocks:

```text
🔧 read(path=README.md) ... done 42ms
```

Add `--show-tool-results` for expanded output.

## [x] 3. Improved streaming layout

Implemented by `0fed3f4 feat(cli): improve streaming status output`.

Keep assistant text flowing naturally. Put system/run metadata on stderr or behind
`--verbose`, and prevent progress logs from interrupting streamed assistant prose.

## [x] 4. Interactive thread commands

Implemented by interactive chat commands:

```text
/new
/threads
/switch <id>
/rename <title>
/copy-id
/export [markdown|json]
/debug
```

## [x] 5. Thread history picker

Implemented by interactive selection for `minigent resume` / `minigent-client resume`
when run in a TTY with multiple remembered threads, plus interactive `/threads` selection
in chat mode. The picker accepts a number, exact thread ID, or unique title/date substring,
and thread lists show title, last updated time, and known message count.

## [x] 6. Transcript export

Implemented by `962e7ed feat(cli): add transcript export command`.

Support transcript export commands:

```bash
minigent export --format markdown
minigent export --format json
```

This is useful for sharing debugging sessions or saving agent work.

## [x] 7. Clearer error messages

Implemented by `e60c987 feat(cli): add friendly error messages`.

Replace raw HTTP/API errors with friendly summaries. For example:

```text
Authentication failed. Check MINIGENT_API_TOKEN.
```

Include `--debug` for full response bodies.

## [x] 8. Config inspection command

Implemented by `844328f feat(cli): add diagnostics commands`.

Add config inspection and validation commands:

```bash
minigent config show
minigent config doctor
```

Mask secrets by default. Validate base URL, token presence, model, broker URL, and
backend mode.

## [x] 9. Connection test

Implemented by `844328f feat(cli): add diagnostics commands`.

Add a ping command:

```bash
minigent ping
```

Report API reachability, auth status, MCP broker availability, and model/backend info.

## [x] 10. Better multiline input

Implemented by interactive chat `prompt_toolkit` support: persistent history, configurable submit behavior, `Esc+Enter`/`Ctrl+J` newline insertion, and `/editor` for long prompts.

In interactive mode:

- Enter sends by default, or make this configurable.
- `Alt+Enter` / `Ctrl+J` inserts a newline.
- `/editor` opens `$EDITOR` for long prompts.

## [x] 11. Shell-friendly non-interactive mode

Implemented by `minigent run` / `minigent-client run`, with stdin prompt input, plain stdout replies by default, structured `--json`, explicit `--plain`, `--no-stream`, `--stream`, and `--quiet` for suppressing streaming progress.

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

## [x] 12. Session resume UX

Implemented by:

- `ca52ec5 feat(cli): add local thread history and resume command`
- `26d9937 feat(client): support thread resume across all modes`

Support session resume commands:

```bash
minigent resume
minigent resume <thread-id>
```

`resume` without an ID should pick the latest thread.

## [x] 13. Debug bundle command

Implemented by `minigent debug-bundle` / `minigent-client debug-bundle`, with masked JSON or human-readable diagnostics and optional `--output`.

Add a debug bundle command:

```bash
minigent debug-bundle
```

Include thread ID, config minus secrets, recent events, backend mode, MCP status, and
version info.

## [x] 14. CLI visual polish

Use subtle ANSI formatting when a TTY is detected. Disable color automatically for pipes
or with `NO_COLOR`. Clearly distinguish:

- user messages
- assistant responses
- tool calls
- warnings
- errors

## [x] 15. Abort handling

Make `Ctrl+C` graceful:

- First press stops the current generation.
- Second press exits.

Print whether the run was locally aborted or server-cancelled. Streaming cancellation now
uses an explicit API cancel request and resets stale running thread state so the next prompt
can run immediately.

## [x] 16. Token count display

Streaming runs print a compact final token summary when usage metadata is available. The CLI now supports `--tokens auto|live|off`, includes structured `usage` fields in streaming `--json` output, and interactive chat has `/tokens` for an estimated current thread size.

Still future polish: context-window percentage when model context metadata is available.

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
