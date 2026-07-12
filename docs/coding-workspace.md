# Coding workspace setup

Minigent can run as a local coding assistant by combining tenant capability profiles with
workspace-scoped MCP servers. The default stack is deliberately read-only; expand it only for
trusted local workspaces.

## Tool boundary: local tools vs MCP tools

Keep Minigent's built-in local tools for low-risk, generic utilities such as `current_time`,
`calculator`, and optional retrieval. Workspace tools should normally be exposed through MCP
servers instead of Minigent local tools.

Use MCP for coding capabilities because they are workspace-specific and high-risk:

- filesystem inspection and editing
- shell commands, test runs, builds, linters, and git operations
- any tool that should be scoped to one workspace root or sandbox

A shell command tool should therefore be an MCP capability, not a default Minigent local tool.
Running shell commands from the Minigent API process would expose the API process environment
and OS permissions. Running shell through a dedicated MCP server keeps the command runner
separate, lets capability profiles decide who can use it, and leaves room for stronger
isolation such as a restricted user, container, or other sandbox.

Recommended profile split:

- `inspect`: read-only filesystem MCP tools, targeted text-read MCP tools, plus safe local utilities
- `edit`: explicit filesystem write/edit MCP tools, if needed
- `test` or `dev`: shell-command MCP tools for trusted local testing/build workflows

Even with MCP, shell access is not a complete sandbox. Treat it as trusted-local-only unless
its MCP server is independently sandboxed and stripped of sensitive environment variables.

## Read-only filesystem access

Prefer filesystem access through MCP servers instead of built-in Minigent local tools. File
access is workspace-specific and high-risk, so keep it behind explicit MCP server config,
workspace-root restrictions, and capability profiles.

A good first filesystem server is the reference package:

```bash
npx -y @modelcontextprotocol/server-filesystem /path/to/workspace
```

That server is stdio-based, while Minigent consumes MCP over Streamable HTTP. Run it behind
an HTTP bridge, restricted to the intended workspace root:

```bash
minigent-mcp-stdio-bridge \
  --name fs-workspace \
  --port 8765 \
  --allowed-tool list_allowed_directories \
  --allowed-tool list_directory \
  --allowed-tool read_file \
  --deny-glob '**/.env*' \
  --deny-glob '**/.git/**' \
  --deny-glob '**/.venv/**' \
  --allow-glob '**/.env*.template' \
  -- \
  npx -y @modelcontextprotocol/server-filesystem /path/to/workspace
```

Then expose it to only the profiles that need codebase access. The optional `allowed_tools`
field on each MCP server narrows the tools Minigent registers and can call from that server;
the example below keeps the workspace profile read-only:

```dotenv
MINIGENT_TENANT_EXECUTION_CONFIGS={
  "demo-tenant":{
    "llm":{"provider":"mock"},
    "tools":{
      "allowed_local_tools":["current_time","calculator"],
      "mcp_servers":[
        {"name":"fs-workspace","url":"http://127.0.0.1:8765/mcp","headers":{},"allowed_tools":["list_allowed_directories","list_directory","read_file"],"path_policy":{"deny_globs":["**/.env*","**/.git/**","**/.venv/**","**/.pytest_cache/**","**/.ruff_cache/**","**/.uv-cache/**"],"allow_globs":["**/.env*.template"]}}
      ]
    },
    "skills":{
      "default_skill":"coding-workspace",
      "items":[
        {
          "name":"coding-workspace",
          "system_prompt":"You are assisting with a code workspace. When the user says current directory, workspace, repo, or repository root, use its absolute path. Filesystem MCP tools require explicit absolute paths; always pass the path argument for directory and file operations. Prefer working with git-tracked source files; use git status or git ls-files when needed to distinguish tracked, untracked, ignored, and generated files. Do not read or write secrets such as .env files unless the user explicitly asks and the active tool policy permits it."
        }
      ]
    },
    "capability_profiles":{
      "default_profile":"inspect",
      "items":[
        {
          "name":"inspect",
          "allowed_local_tools":["current_time","calculator"],
          "mcp_server_names":["fs-workspace"]
        }
      ]
    }
  }
}
```

Create a thread with that profile:

```bash
uv run python scripts/demo_client.py \
  --tenant-id demo-tenant \
  --capability-profile inspect \
  "list the files in this workspace"
```

For stricter read/edit/test separation, run separate MCP servers or a filtering bridge and map
them to separate profiles such as `inspect`, `edit`, and `test`.

## Convenience runner

To run this as a reusable local coding-assistant stack, copy the coding env template and start
the convenience runner. It loads `.env.coding` by default, starts the filesystem stdio bridge,
starts the Minigent API, and prints a ready-to-run demo client command:

```bash
cp .env.coding.template .env.coding
# edit MINIGENT_CODING_WORKSPACES=/path/to/workspace
uv run minigent-coding-workspace --env-file .env.coding
```

Use `uv run minigent-coding-workspace --no-env-file` when you want to inherit only the
process environment and unified config, without reading a coding dotenv file.

### Optional encrypted coding env with SOPS

For local real-LLM demos, you can keep API keys and other coding-workspace settings in an
encrypted dotenv file instead of a plaintext `.env.coding`. Create a temporary plaintext file
from the template, edit it locally, encrypt it with your age recipient, then delete the
plaintext copy:

```bash
cp .env.coding.template .coding.env
# edit .coding.env with workspace paths and provider settings such as OPENROUTER_API_KEY
sops --config /dev/null \
  --encrypt \
  --input-type dotenv \
  --output-type dotenv \
  --age "$(age-keygen -y "$HOME/.config/sops/age/keys.txt")" \
  .coding.env > .coding.sops.env
rm .coding.env
chmod 600 .coding.sops.env
```

Run the coding-workspace stack by decrypting the SOPS file into the child process
environment. `--no-env-file` keeps the runner from also loading `.env.coding`:

```bash
SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt" \
sops exec-env .coding.sops.env \
  'uv run minigent-coding-workspace --no-env-file'
```

Use the same pattern for one-off real-provider skill demos by pointing `MINIGENT_CONFIG_FILE`
at a local TOML config and keeping provider keys in `.coding.sops.env`. Do not commit
decrypted dotenv files or place API keys in `minigent.toml`; `.coding.sops*.env` is ignored
for local encrypted dotenv files.

The template sets `MINIGENT_THREAD_DB_PATH=.data/minigent-coding-threads.db` so coding threads
survive runner/API restarts. Remove that setting only if you intentionally want in-memory,
restart-discarded threads.

To expose multiple roots through the same filesystem MCP server, set
`MINIGENT_CODING_WORKSPACES` to a comma-separated list or repeat `--workspace` on the runner
CLI. The older singular `MINIGENT_CODING_WORKSPACE` key is still accepted for compatibility.
Generated tenant configs include the resolved roots in the coding-workspace skill prompt so
the model can distinguish each configured workspace root:

```dotenv
MINIGENT_CODING_WORKSPACES=/path/to/repo1,/path/to/repo2
```

```bash
uv run minigent-coding-workspace --workspace /path/to/repo1 --workspace /path/to/repo2
```

When trusted-local shell support is enabled, shell `cwd` values may be under any configured
workspace root and default to the first root.

### Workspace scopes

When a runner config exposes multiple workspace roots, you can define named scopes in
`minigent.toml` and select one scope for the run. The MVP scope behavior is advisory: it
narrows the roots passed to the runner-generated MCP server commands and coding skill prompt,
but it is not a standalone security boundary for already-running external tools. Keep the
outer `coding.workspaces` list as the broad set of allowed roots; scope roots should sit
inside those configured roots.

```toml
[coding]
workspaces = ["/Users/example/code", "/Users/example/dotfiles"]
default_workspace_scope = "minigent"

[coding.workspace_scopes.minigent]
roots = ["/Users/example/code/minigent"]
description = "Minigent runtime and coding workspace development"

[coding.workspace_scopes.dotfiles]
roots = ["/Users/example/dotfiles"]
description = "Personal shell/editor configuration"
```

Resolution order is:

1. `--workspace-scope` or `MINIGENT_CODING_WORKSPACE_SCOPE`;
2. the active default skill's `workspace_scope` / `workspaceScope`, when present in tenant config;
3. `coding.default_workspace_scope` / `MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE`;
4. all configured workspace roots when no scope is selected.

Unknown scope names fail before the runner starts. When a scope is active, the generated
coding prompt includes `Active workspace scope: <name>` and tells the model to stay within
those roots unless the user explicitly asks to switch scope.

The runner starts the bridge with read-only filesystem tools by default. If you provide
`MINIGENT_TENANT_EXECUTION_CONFIGS` with an `allowed_tools` list for the configured
`fs-workspace` server, the runner mirrors that list into the bridge's `--allowed-tool` filter
so fuller coding profiles can expose additional filesystem MCP tools. It also mirrors the
server `path_policy` into the bridge's `--deny-glob` and `--allow-glob` filters. You can
override the bridge path policy directly with comma-separated globs:

```dotenv
MINIGENT_CODING_BRIDGE_DENY_GLOBS=**/.env*,**/.git/**,**/.venv/**,**/.pytest_cache/**,**/.ruff_cache/**,**/.uv-cache/**
MINIGENT_CODING_BRIDGE_ALLOW_GLOBS=**/.env*.template,**/.env*.driver.sh
```

Direct bridge env vars take precedence over the mirrored tenant path policy.

### Declarative MCP server specs

For more than the built-in filesystem/text/shell tools, keep MCP server launch/connect
definitions directly in `minigent.toml` with `[[coding.mcp_server_specs]]`. This keeps the
unified config self-contained instead of pointing at a secondary MCP server file. Each server
entry can define:

- `name`: MCP server name registered in tenant config.
- `transport`: `stdio` to start it behind the stdio bridge/gateway, or `http` to register an
  HTTP MCP server. Defaults to `stdio`.
- `command`: argv for stdio servers and for managed HTTP servers. Use `{workspace}` for the
  first workspace, `{workspace_roots}` as an argv item that expands to all workspace roots,
  `{workspace_roots_csv}` for a comma-separated root list, or `{workspace_args}` to expand
  to repeated `--workspace <root>` pairs. For compatibility, `--workspace {workspace}` also
  expands to one `--workspace <root>` pair for each active workspace root.
- `host`, `port`, and `path`: local bridge bind settings for `http` servers and for the
  legacy compatibility mode where the runner starts one bridge process per stdio server. The
  tenant `url` defaults to `http://<host>:<port><path>` unless `url` is set explicitly.
  When `MINIGENT_CODING_MCP_GATEWAY_ENABLED=true`, stdio server entries do not need these
  fields; generated tenant URLs use the shared gateway path `/<prefix>/<server-name>`.
- `profiles`: capability profiles that should include this server, such as `inspect`, `edit`,
  or `test`.
- `allowed_tools` and `path_policy`: tool filters and bridge/tool path filters.
- `env`: extra process environment for stdio servers and managed HTTP servers.
- `headers`: HTTP headers to send when Minigent calls the server URL.
- `managed`: for `transport: "http"`, start `command` as a child process before Minigent.
  Defaults to `false`; unmanaged HTTP entries are only registered as external endpoints.
- `health_url`: optional URL to poll for a managed HTTP server before starting the API.
- `startup_timeout_seconds`: optional managed HTTP health-check timeout; defaults to `30`.
- `request_timeout`: optional stdio bridge/gateway timeout while waiting for one MCP response;
  defaults to `30`.
- `timeout_seconds`: optional Minigent HTTP client timeout for calls to this MCP server;
  defaults to `request_timeout` for coding MCP server specs and to `30` in tenant configs.

- `restart_on_timeout`: optional bool for stdio gateway entries. When true, the gateway
  restarts that stdio subprocess after a bridge read/write timeout or request cancellation;
  this is useful for state-light servers such as `shell-workspace` where a long-running
  command can otherwise block later requests behind the serialized stdio request lock.
  Defaults to `false`.

String values in `command`, `env`, `headers`, `url`, and `health_url` can reference dotenv or
environment values with `${NAME}` placeholders. Prefer passing credentials through `env` or
`headers` rather than command-line arguments so they are not exposed in process listings.

Example:

```toml
[[coding.mcp_server_specs]]
name = "fs-workspace"
command = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "{workspace_roots}"]
profiles = ["inspect"]
allowed_tools = ["list_allowed_directories", "list_directory", "read_file"]
path_policy = { deny_globs = ["**/.env*", "**/.git/**", "**/.venv/**"], allow_globs = ["**/.env*.template"] }

[[coding.mcp_server_specs]]
name = "custom-workspace"
command = ["custom-mcp-server", "{workspace_args}"]
profiles = ["inspect", "test"]
allowed_tools = ["inspect_repo", "run_repo_check"]

[[coding.mcp_server_specs]]
name = "web-search"
transport = "http"
managed = true
command = [
  "npx",
  "-y",
  "@brave/brave-search-mcp-server",
  "--transport",
  "http",
  "--host",
  "127.0.0.1",
  "--port",
  "8766",
]
url = "http://127.0.0.1:8766/mcp"
health_url = "http://127.0.0.1:8766/ping"
env = { BRAVE_API_KEY = "${BRAVE_API_KEY}" }
profiles = ["inspect"]
allowed_tools = ["brave_web_search", "brave_news_search", "brave_llm_context"]

[[coding.mcp_server_specs]]
name = "remote-company-tools"
transport = "http"
url = "https://mcp.example.com/mcp"
headers = { Authorization = "Bearer ${COMPANY_MCP_TOKEN}" }
profiles = ["inspect", "edit"]
```

### Optional codebase-memory-mcp graph navigation

[`codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp) can be added as an
optional code-navigation layer for structural discovery. Use it to find relevant symbols,
call paths, routes, architecture boundaries, and change-impact candidates before doing exact
filesystem/text reads. It is not a replacement for the filesystem MCP: before editing an
existing file, verify the current contents through `fs-workspace` or `text-workspace`.

Install the `codebase-memory-mcp` binary separately and prefer an install mode that does not
modify your editor/agent configuration automatically. For example, download a release binary
or use the project's installer options such as `--skip-config` when appropriate, then make
sure `codebase-memory-mcp` is on `PATH` or use an absolute command path in `minigent.toml`.

Add it to `[[coding.mcp_server_specs]]` alongside filesystem/text/shell tools:

```toml
[[coding.mcp_server_specs]]
name = "fs-workspace"
command = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "{workspace_roots}"]
profiles = ["inspect"]
allowed_tools = ["list_allowed_directories", "list_directory", "read_file"]
path_policy = { deny_globs = ["**/.env*", "**/.git/**", "**/.venv/**"], allow_globs = ["**/.env*.template"] }

[[coding.mcp_server_specs]]
name = "codebase-memory"
command = ["codebase-memory-mcp"]
profiles = ["inspect"]
allowed_tools = [
  "index_repository",
  "search_graph",
  "search_code",
  "semantic_query",
  "get_architecture",
  "trace_call_path",
  "detect_changes",
]
```

For multi-server setups, prefer the shared gateway:

```dotenv
MINIGENT_CODING_MCP_GATEWAY_ENABLED=true
MINIGENT_CODING_MCP_GATEWAY_PORT=8765
MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX=/mcp
```

Start the runner as usual:

```bash
uv run minigent-coding-workspace --env-file .env.coding
```

To export a restartable TOML for the full local coding stack, merge the API-owned config with
the locally resolved runner config:

```bash
uv run minigent --env-file .env.coding config export --local-coding --output minigent.toml
# Equivalent coding-runner wrapper:
uv run minigent-coding-workspace config export --env-file .env.coding --output minigent.toml
# Export without reading a coding dotenv file:
uv run minigent-coding-workspace config export --no-env-file --output minigent.toml
```

On first use, ask the coding agent to index the repository, for example: "Index this
project." After that, prefer this workflow:

1. Use `codebase-memory` tools for discovery and impact analysis.
2. Use `text-workspace`, when configured, for exact line ranges around the returned files/symbols.
3. Use `fs-workspace` for broader authoritative reads and all edits.
4. Re-index or refresh graph queries after meaningful changes when graph freshness matters.

The exact `allowed_tools` list should match the installed `codebase-memory-mcp` release. If a
listed tool is unavailable, remove it from `allowed_tools` or omit `allowed_tools` to expose the
server's full tool list to the selected profile.

When the runner generates `MINIGENT_TENANT_EXECUTION_CONFIGS`, it derives
`tools.mcp_servers` and `capability_profiles.items[*].mcp_server_names` from the inline
`coding.mcp_server_specs`. If you provide `MINIGENT_TENANT_EXECUTION_CONFIGS` yourself, the
inline specs still control process startup, but your explicit tenant config remains
authoritative for tool registration and profiles.

By default, each stdio server still runs behind its own local bridge/port for backwards
compatibility; if `port` is omitted, the runner assigns sequential local bridge ports. For new
multi-server setups, prefer a single local gateway process:

```dotenv
MINIGENT_CODING_MCP_GATEWAY_ENABLED=true
MINIGENT_CODING_MCP_GATEWAY_PORT=8765
MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX=/mcp
```

With the gateway enabled, generated tenant config uses URLs shaped like:

```text
http://127.0.0.1:8765/mcp/fs-workspace
http://127.0.0.1:8765/mcp/text-workspace
http://127.0.0.1:8765/mcp/shell-workspace
```

If you provide `MINIGENT_TENANT_EXECUTION_CONFIGS` yourself, update the `tools.mcp_servers`
URLs to the gateway paths; the runner does not rewrite explicit tenant config. In gateway
mode, per-server `host`, `port`, `path`, and `url` fields in the stdio server specs are legacy
compatibility settings and are not needed unless you also run without the gateway.

If an explicit tenant config references a gateway URL like `/mcp/text-workspace` but no matching
`[[coding.mcp_server_specs]]` entry or legacy MCP server-file entry was loaded, the runner prints
a warning before startup because those calls would otherwise fail with gateway 404 responses.

You can also run the gateway directly with a gateway config file:

```bash
uv run minigent-mcp-stdio-gateway --config .data/mcp-gateway.json --port 8765
```

### Targeted text reads

The convenience runner can also start Minigent's small targeted text-read MCP server. This
server complements the authoritative filesystem MCP by exposing efficient exact reads for
known files and regions:

- `read_text_file_lines(path, start_line, end_line)` reads an inclusive 1-based line range.
- `read_text_file_around(path, line, before, after)` reads context around a 1-based line.
- `search_text_file(path, pattern, before, after, max_matches)` searches within one text file
  and returns matching line contexts.

Enable it with `MINIGENT_CODING_TEXT_ENABLED=true` or `--enable-text`. When the runner
generates the tenant config, it starts a second read-only MCP bridge named `text-workspace`
on port `8767` and adds it to the default `inspect` capability profile:

```bash
uv run minigent-coding-workspace --env-file .env.coding --enable-text
uv run python scripts/demo_client.py \
  --tenant-id demo-tenant \
  --capability-profile inspect \
  '/tool text-workspace.read_text_file_around {"path":"/path/to/workspace/README.md","line":1,"after":20}'
```

The targeted text server requires paths to stay under one of the configured workspace roots
and reads UTF-8 text files only. It is for inspection, not mutation; keep using the filesystem
MCP layer as the source of truth for file writes and broader file operations.

To enable targeted text reads from `.env.coding`, use:

```dotenv
MINIGENT_CODING_TEXT_ENABLED=true
MINIGENT_CODING_TEXT_BRIDGE_NAME=text-workspace
MINIGENT_CODING_TEXT_BRIDGE_PORT=8767
```

If you provide `MINIGENT_TENANT_EXECUTION_CONFIGS` yourself, include the text MCP server in
`tools.mcp_servers` and add it to the relevant capability profile. For example:

```json
{
  "demo-tenant": {
    "llm": {"provider": "mock"},
    "tools": {
      "allowed_local_tools": ["current_time", "calculator"],
      "mcp_servers": [
        {
          "name": "fs-workspace",
          "url": "http://127.0.0.1:8765/mcp",
          "headers": {},
          "allowed_tools": ["list_allowed_directories", "list_directory", "read_file"],
          "path_policy": {
            "deny_globs": ["**/.env*", "**/.git/**", "**/.venv/**"],
            "allow_globs": ["**/.env*.template"]
          }
        },
        {
          "name": "text-workspace",
          "url": "http://127.0.0.1:8767/mcp",
          "headers": {},
          "allowed_tools": [
            "read_text_file_lines",
            "read_text_file_around",
            "search_text_file"
          ],
          "path_policy": {
            "deny_globs": ["**/.env*", "**/.git/**", "**/.venv/**"],
            "allow_globs": ["**/.env*.template"]
          }
        }
      ]
    },
    "capability_profiles": {
      "default_profile": "inspect",
      "items": [
        {
          "name": "inspect",
          "allowed_local_tools": ["current_time", "calculator"],
          "mcp_server_names": ["fs-workspace", "text-workspace"]
        }
      ]
    }
  }
}
```

To enable trusted-local shell commands, set `MINIGENT_CODING_SHELL_ENABLED=true` or pass
`--enable-shell`. When the runner generates the tenant config, this starts a second MCP bridge
named `shell-workspace` on port `8766` and adds a non-default `test` capability profile that
can call `shell-workspace.run_command`:

```bash
uv run minigent-coding-workspace --env-file .env.coding --enable-shell
uv run python scripts/demo_client.py \
  --tenant-id demo-tenant \
  --capability-profile test \
  '/tool shell-workspace.run_command {"command":"uv run pytest","cwd":"/path/to/workspace"}'
```

The shell MCP server requires command working directories to stay under one of the configured
workspace roots, passes through only a small environment allowlist, disables stdin, enforces a
timeout, and truncates stdout/stderr. Keep `[app].tool_timeout_seconds` greater than or equal
to the shell MCP `request_timeout`/`timeout_seconds`; `minigent config doctor` warns when the
outer runtime timeout is shorter. Commands run through `/bin/sh` by default. If you define
`shell-workspace` explicitly in unified config and want zsh, configure the server command itself:

```toml
[[coding.mcp_server_specs]]
name = "shell-workspace"
transport = "stdio"
command = [
  "python",
  "-c",
  "from app.shell_mcp_server import main; raise SystemExit(main())",
  "{workspace_args}",
  "--shell",
  "/bin/zsh",
]
request_timeout = 180
timeout_seconds = 180
restart_on_timeout = true
```

You can also add a command-prefix allowlist:

```dotenv
MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES=git,rg,find,ls,pwd,uv run pytest,uv run ruff check,uv run basedpyright
```

That blocks commands whose strings do not exactly match or start with one of those prefixes,
such as `cat .env`. Treat this as defense-in-depth, not a sandbox: broad prefixes such as
`git` or `python` can still have surprising effects, and shell syntax is flexible. Only enable
shell for trusted local workspaces or run the bridge/server inside a separate sandbox.

`.env.coding.template` also includes a commented Generic OAuth LLM example for coding profiles.
Uncomment it, fill in the OAuth/provider values, start the runner, then open
`http://127.0.0.1:8000/oauth/generic/open` to authorize the LLM provider.

## Smoke test

To smoke-test the flow without creating an env file, run the one-shot filesystem MCP demo
script. It starts the same bridge and a local Minigent API process, creates an `inspect`
thread, then calls the filesystem MCP `list_directory` and `read_file` tools through
Minigent's mock adapter:

```bash
uv run python scripts/demo_filesystem_mcp.py --workspace /path/to/workspace
```
