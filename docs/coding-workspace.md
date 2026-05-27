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
the convenience runner. It loads `.env.coding`, starts the filesystem stdio bridge, starts the
Minigent API, and prints a ready-to-run demo client command:

```bash
cp .env.coding.template .env.coding
# edit MINIGENT_CODING_WORKSPACES=/path/to/workspace
uv run minigent-coding-workspace --env-file .env.coding
```

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

### Declarative MCP server lists

For more than the built-in filesystem/text/shell tools, point the runner at a JSON MCP server
list instead of adding more runner-specific flags. Relative paths are resolved from the dotenv
file directory:

```dotenv
MINIGENT_CODING_MCP_SERVERS_FILE=.data/coding-mcp-servers.json
```

The file can be a JSON array or an object with a `servers` array. Each server entry can define:

- `name`: MCP server name registered in tenant config.
- `transport`: `stdio` to start it behind the stdio bridge, or `http` to register an
  externally managed HTTP MCP server. Defaults to `stdio`.
- `command`: argv for stdio servers. Use `{workspace}` for the first workspace,
  `{workspace_roots}` as an argv item that expands to all workspace roots, or
  `{workspace_roots_csv}` for a comma-separated root list.
- `host`, `port`, and `path`: local bridge bind settings for `http` servers and for the
  legacy compatibility mode where the runner starts one bridge process per stdio server. The
  tenant `url` defaults to `http://<host>:<port><path>` unless `url` is set explicitly.
  When `MINIGENT_CODING_MCP_GATEWAY_ENABLED=true`, stdio server entries do not need these
  fields; generated tenant URLs use the shared gateway path `/<prefix>/<server-name>`.
- `profiles`: capability profiles that should include this server, such as `inspect`, `edit`,
  or `test`.
- `allowed_tools`, `path_policy`, and `env`: bridge/tool filters and additional process
  environment.

Example:

```json
{
  "servers": [
    {
      "name": "fs-workspace",
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "{workspace_roots}"],
      "profiles": ["inspect"],
      "allowed_tools": ["list_allowed_directories", "list_directory", "read_file"],
      "path_policy": {
        "deny_globs": ["**/.env*", "**/.git/**", "**/.venv/**"],
        "allow_globs": ["**/.env*.template"]
      }
    },
    {
      "name": "custom-workspace",
      "command": ["custom-mcp-server", "--workspace", "{workspace}"],
      "profiles": ["inspect", "test"],
      "allowed_tools": ["inspect_repo", "run_repo_check"]
    }
  ]
}
```

When the runner generates `MINIGENT_TENANT_EXECUTION_CONFIGS`, it derives
`tools.mcp_servers` and `capability_profiles.items[*].mcp_server_names` from this file. If you
provide `MINIGENT_TENANT_EXECUTION_CONFIGS` yourself, the file still controls process startup,
but your explicit tenant config remains authoritative for tool registration and profiles.

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
mode, per-server `host`, `port`, `path`, and `url` fields in the stdio server-list file are
legacy compatibility settings and are not needed unless you also run without the gateway.

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
timeout, and truncates stdout/stderr. You can also add a command-prefix allowlist:

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
