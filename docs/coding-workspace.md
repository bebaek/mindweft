# Coding workspace setup

Mindweft can run as a local coding assistant by combining tenant capability profiles with
workspace-scoped MCP servers. The default stack is deliberately read-only; expand it only for
trusted local workspaces.

## Simple read-only launcher

`mindweft code` is the small, trusted-local entry point for an already-configured user:

```bash
mindweft code                         # current directory
mindweft code /absolute/path/to/repo
mindweft code /path/to/repo1 /path/to/repo2
mindweft code . --no-open             # headless / SSH
mindweft code . --port 8080 --gateway-port 8768
mindweft code . --demo                # explicit mock provider, no real AI responses
```

Requirements: the installed Mindweft package with its console assets on macOS/Linux, and provider
settings unless `--demo` is used. The filesystem and text servers ship in the Python package and
run using the same interpreter as Mindweft; Node/npm is not required at runtime.
For a source checkout, use `./scripts/dev.py code . --instance preview` to build and stage
the console before launching the checkout. `./scripts/dev.py build` only builds/stages;
`./scripts/dev.py install` reinstalls the uv tool (Python 3.12 by default); its wheel-build
hook automatically compiles the console, as does `uv tool install --reinstall --python 3.12 .`.
Source builds require Node.js/npm and fail rather than reuse stale assets.
Build failures stop before launch/install; `code` does not modify the installed tool. Stop
the target instance before restarting or reinstalling it. End users of the built wheel do
not build the frontend.

The command resolves and deduplicates the explicitly supplied directories (or cwd when omitted).
Every directory must be accessible; one invalid root aborts startup before any child starts.
All approved roots are printed and passed to both the built-in filesystem inspection
and targeted text servers behind the shared gateway. The launcher generates only an `inspect` profile.
Writes, shell, external MCP servers, peer backends, admin execution overlays, and inherited
workspace scopes are not enabled. Existing path deny-glob defaults remain in force. This is
not an OS sandbox or a guarantee that every possible secret filename is excluded.

### Credential-backed local authentication

`mindweft code` protects both the API and MCP gateway with separate 256-bit opaque bearer
credentials, rotated on every launch. Credentials live under the selected state root's
`instances/.credentials/<name>/{api,gateway}/credential`, separate from public discovery metadata.
The credential directories/files must be owned by the current OS user with modes `0700`/`0600`.
Unsafe ownership, writable ancestors, symlinks, hardlinks, and malformed files are rejected instead
of repaired. Workspace tools deny `.credentials` paths. Development-header authentication is not
a fallback in this launcher mode; ordinary deployed session/bearer authentication is unchanged.

CLI `--instance NAME` reads the protected API credential and uses only the matching literal
loopback origin, without proxies or redirects. The server maps it to the local `demo-tenant` /
`demo-user` principal (not admin), regardless of caller-supplied principal headers. Server verifiers
retain token digests. Replacing a credential file alone does not revoke a running verifier;
restart the instance to rotate credentials and invalidate its browser sessions.

The launcher opens `/console/` with a 30-second single-use ticket in the URL fragment. The console
removes that fragment before other requests and exchanges the ticket for an HttpOnly, host-only,
SameSite=Strict cookie plus an origin-scoped session key. Sessions expire after eight hours or
on restart. Literal loopback HTTP does not use a Secure cookie. Because cookies are shared across
ports, protected browser requests also require the session key and launch ID; the cookie alone
is insufficient. The key is kept in tab session storage (memory only if storage is unavailable),
scoped by instance name and launch ID. Cookies and session keys cannot mint new browser tickets.
Host/Origin checks and same-origin write requirements guard browser requests.

Use `mindweft instances open NAME` to open another authenticated browser session. Ticket URLs are
not printed if opening the browser fails; retry from a desktop session. Expired sessions or
restarted instances require reopening from the CLI. Anonymous direct visits show reconnect
instructions, not a password form. The browser never receives the reusable local bearer token.
No authentication downgrade is inferred from a 401.

This protects the local service boundary from other unprivileged OS users under normal filesystem
and browser-profile isolation, not root or malicious same-user processes. The CLI is a trusted
bootstrapper and can recreate browser sessions with the same authority. Keep services loopback-only.
Provider OAuth authentication is separate; new provider-login flows need follow-up work.

### Multiple local instances

Use an explicit name when testing alongside an existing instance:

```bash
# Keep the installed daily-use version running.
mindweft code /path/to/project --instance daily

# In a separate, updated checkout/environment:
./scripts/dev.py code /path/to/project --instance preview

# Connect a CLI to the matching instance, without remembering its port:
mindweft --instance preview chat "Explain this repository"
mindweft instances list
mindweft instances open preview
```

Use the updated checkout's `uv run mindweft ...` for client commands too if the installed tool
predates `--instance`; an already-running older instance need not be restarted or reinstalled.

Add `--demo` to either launch for mock-only testing. Instance names contain 1-64 letters,
digits, underscores, or hyphens, starting with a letter/digit. `--instance` goes after `code`
for launching and before the client subcommand for connecting. One name can run only once.

**Ports:** named launches prefer API port 8000 and gateway port 8765. If an omitted port is
occupied, the OS allocates a free loopback port. The launcher holds listening sockets and passes
their descriptors to the child servers, avoiding a check-then-bind race. Explicit `--port` and
`--gateway-port` values are strict: they must be distinct, within 1-65535, and available. The
actual API URL is printed/opened; actual gateway URLs are used by the generated tool configuration.

**State:** omitting `--instance` selects `default` and preserves existing storage settings and
legacy state-directory fallback. Other names store threads and attachments under
`$XDG_STATE_HOME/mindweft/instances/<name>` (normally `~/.local/state/mindweft/instances/<name>`).
Named instances override inherited thread/attachment paths, reporting the overridden setting names.
Provider configuration and an explicitly configured `MINDWEFT_OAUTH_STORE_PATH` (or legacy
alias) are reused unchanged, including OAuth encryption settings. Credentials
are not copied, migrated, or inspected by the launcher. Without a configured OAuth store, the named
instance still gets an instance-local `oauth.json`/`oauth.db`; existing instance-local OAuth files
are not merged into a configured shared store. Local API/gateway credentials remain instance-specific.

An explicitly shared OAuth store is exempt from process-lifetime storage locks. Encrypted SQLite
OAuth stores already coordinate refreshes. **JSON stores do not coordinate cross-process token
refresh: avoid simultaneous provider runs across instances sharing a JSON store.** The launcher
prints a warning for this case. Sharing a path does not validate or renew provider credentials;
expired/revoked or missing credentials can still fail. The OAuth login flow is separate work.
Other default
XDG state is directed into the instance's own `xdg` subdirectory. Default in-memory stores remain
in memory. Reusing a name after shutdown reuses its persistent data; do not run incompatible
versions against the same named state.

**Legacy safety:** older launchers and advanced runners do not participate in these instance/store
locks. Use a new name such as `preview` when testing beside them. An unnamed/default launch refuses
automatic API-port fallback if 8000 is occupied, rather than silently sharing a possible older
instance's databases. An explicit port does not isolate default storage; prefer a non-default name.

**Discovery:** each ready instance atomically publishes an owner-only JSON record in the user's
state directory under `instances/`. It contains the name, launch ID, API/gateway URLs, launcher
PID, and version, not credentials. Nothing is published until API/tool/identity readiness passes.
Named clients verify the name and fresh launch ID against `/local-instance`; stale/unavailable
records never fall back to another instance. Requests then carry a launch-ID header, which
participating servers reject if the identity changed. Identity verification is not authentication;
normal principal headers/authentication still apply. Named CLI history is scoped by instance name
rather than its changing port.

`--instance` and an explicit `--base-url` cannot be combined. An explicitly selected instance takes
precedence over environment URL defaults. Without `--instance`, clients retain their existing URL
behavior; automatic client selection is not included in this slice. `instances list` distinguishes
running from stale records; `instances open NAME` verifies identity before opening the console.

Locks protect both instance names and resolved storage paths used by new launchers. Service
children inherit lock descriptors, so a killed launcher cannot release a name/store while its
services are still alive. Normal shutdown removes its own registry record and stops only its own
children. Lock files intentionally remain: do not delete them to force a second launch.

**Separate the code too:** keep your daily version in a non-editable installation and test updates
from a separate checkout/worktree and environment. Reinstalling over a running tool or editing a
shared editable checkout can replace code/assets that an existing process still uses. Instance
isolation does not snapshot the installed code or repository files.

The console opens at the correct instance origin, but automatic console authentication is a
separate follow-up. For now choose Development headers, tenant `demo-tenant`, user `demo-user`
via Configure. Different ports isolate browser origins/local storage, **not cookies**.

### Configuration and authentication

Startup prints `Config: loaded '<path>'` to stderr after TOML loading succeeds. For a symlink,
`Config target: '<resolved-path>'` identifies its target (including dotfiles-managed configs).
If no file is loaded, it reports `environment only` and distinguishes disabled discovery from
no user-level TOML found. This diagnostic contains paths only, never configuration values or
credentials, and appears even if later provider preflight fails. A scope reminder makes clear
that loading a file does not enable its custom tools, tenants, skills, or workspace settings.


- Loads an explicitly selected `MINDWEFT_CONFIG_FILE` (legacy alias accepted), or the canonical
  then legacy user-level TOML path. It does **not** discover cwd-local TOML files.
- Environment overrides TOML. Only provider, authentication, and thread/attachment persistence
  settings from the Mindweft configuration namespace are carried into this mode; tool, tenant,
  skill, and workspace settings are intentionally not inherited.
- Does not load `.env`, `.env.coding`, `--env-file`, or expand `*_FILE` secret references.
  Export provider keys yourself. Child processes cannot rediscover these config files.
- Configured relative database paths resolve against the TOML directory; environment-supplied
  relative database paths resolve against the caller's cwd. Defaults use the existing durable
  user state directory. No configuration is generated in the project.
- A real provider is required unless `--demo` is explicit. Preflight checks configuration
  prerequisites, **not** live credentials or model availability. Provider onboarding is deferred.
- This slice supports the existing development-header authentication only. Token/JWT/session
  configurations are rejected rather than silently weakened; use the advanced runner for those.
  In the console connection settings, use development authentication with tenant `demo-tenant`
  and user `demo-user`.

Both services bind to `127.0.0.1`. Development-header authentication and the local MCP gateway
are **trusted-local only**: do not forward their ports, expose them on a shared host, or treat
loopback binding as an authentication boundary.

### Startup and troubleshooting

The launcher reserves ports, starts the existing runner processes in a temporary working directory,
and waits up to approximately 60 seconds for API readiness, console assets, the inspect profile,
and exact expected MCP tool lists. Only then does it print `Ready` and open the browser. Browser
failure is nonfatal; open the printed URL yourself. `Ctrl+C` or SIGTERM shuts down managed children
and removes the temporary gateway configuration. Startup failure also triggers cleanup.

- **Missing provider:** use `mindweft config init --help` to create a user-level starter config,
  then configure a real provider and export its credentials. `mindweft config doctor` can help,
  but follows the advanced command's normal discovery rules; point it at the same explicit config.
- **Port conflict:** omit port flags for automatic allocation with a named instance, or select different
  explicit `--port` / `--gateway-port` values. A default instance with port 8000 occupied asks for
  a non-default `--instance` name to avoid sharing legacy state.
- **Readiness failure:** inspect child startup output. A healthy API alone is not enough; missing tools
  or absent console assets prevent a ready announcement.

For editing, shell, custom tools, or different authentication, continue using
`mindweft-coding-workspace`; its flags and configuration discovery behavior are unchanged.

### Packaged read-only tools

The simple launcher uses `mindweft_workspace.servers.filesystem`, exposing exactly
`list_allowed_directories`, `list_directory`, and `read_file`, plus the existing targeted-text
server with `--safe-reads`. All commands use the installed Python interpreter, not PATH lookups.

Both servers check supplied **and resolved** paths against the configured roots and default deny/
allow globs. Safe in-root and cross-approved-root symlinks work; symlinks to excluded paths or
outside all approved roots fail. POSIX no-follow descriptor traversal rejects symlink swaps
between validation and open. Reads reject non-regular files (including FIFOs), non-UTF-8/binary
text, and files larger than 1 MiB. Reads return at most 40000 characters; directory listing scans
at most 1000 entries, hides denied/escaping entries, and reports truncation. Relative paths use
the first approved root. Use absolute paths when working with multiple roots.

This is still trusted-local inspection, not hostile-process isolation: hard links and concurrent
renames of already-open directories are not sandboxed. The path policy does not identify every
possible secret filename. The advanced runner and standalone text server retain their existing
behavior unless this safe-read mode is explicitly selected.

The following sections describe the **advanced runner**. Its default npm filesystem backend and
custom MCP server specs remain compatible, including existing editing setups. Only `mindweft code`
selects the packaged read-only backend automatically.

## Tool boundary: local tools vs MCP tools

Keep Mindweft's built-in local tools for low-risk, generic utilities such as `current_time`,
`calculator`, and optional retrieval. Workspace tools should normally be exposed through MCP
servers instead of Mindweft local tools.

Use MCP for coding capabilities because they are workspace-specific and high-risk:

- filesystem inspection and editing
- shell commands, test runs, builds, linters, and git operations
- any tool that should be scoped to one workspace root or sandbox

A shell command tool should therefore be an MCP capability, not a default Mindweft local tool.
Running shell commands from the Mindweft API process would expose the API process environment
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

Prefer filesystem access through MCP servers instead of built-in Mindweft local tools. File
access is workspace-specific and high-risk, so keep it behind explicit MCP server config,
workspace-root restrictions, and capability profiles.

A good first filesystem server is the reference package:

```bash
npx -y @modelcontextprotocol/server-filesystem /path/to/workspace
```

That server is stdio-based, while Mindweft consumes MCP over Streamable HTTP. Run it behind
an HTTP bridge, restricted to the intended workspace root:

```bash
mindweft-mcp-stdio-bridge \
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
field on each MCP server narrows the tools Mindweft registers and can call from that server;
the example below keeps the workspace profile read-only:

```dotenv
MINDWEFT_TENANT_EXECUTION_CONFIGS={
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
starts the Mindweft API, and prints a ready-to-run demo client command:

```bash
cp .env.coding.template .env.coding
# edit MINDWEFT_CODING_WORKSPACES=/path/to/workspace
uv run mindweft-coding-workspace --env-file .env.coding
```

Use `uv run mindweft-coding-workspace --no-env-file` when you want to inherit only the
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
  'uv run mindweft-coding-workspace --no-env-file'
```

Use the same pattern for one-off real-provider skill demos by pointing `MINDWEFT_CONFIG_FILE`
at a local TOML config and keeping provider keys in `.coding.sops.env`. Do not commit
decrypted dotenv files or place API keys in `minigent.toml`; `.coding.sops*.env` is ignored
for local encrypted dotenv files.

The coding workspace runner defaults thread and image attachment storage to
`$XDG_STATE_HOME/mindweft/threads.db` and `$XDG_STATE_HOME/mindweft/attachments.db`, falling
back to `~/.local/state/mindweft/threads.db` and
`~/.local/state/mindweft/attachments.db`. If the Mindweft directory does not yet exist but the
corresponding `$XDG_STATE_HOME/minigent` directory does, the runner keeps using that legacy
directory until it is moved explicitly. This keeps threads and image references valid across
local API restarts without automatically moving live SQLite files. Set `MINDWEFT_THREAD_DB_PATH`
(or legacy `MINIGENT_THREAD_DB_PATH`) or `[app].thread_db_path`, and
`MINDWEFT_ATTACHMENT_DB_PATH` (or legacy `MINIGENT_ATTACHMENT_DB_PATH`) or
`[attachments].db_path`, only to override those coding-runner defaults. Non-coding Mindweft API
deployments retain their existing explicit storage behavior.

To expose multiple roots through the same filesystem MCP server, set
`MINDWEFT_CODING_WORKSPACES` to a comma-separated list or repeat `--workspace` on the runner
CLI. The older singular `MINDWEFT_CODING_WORKSPACE` key is still accepted for compatibility.
Generated tenant configs include the resolved roots in the coding-workspace skill prompt so
the model can distinguish each configured workspace root:

```dotenv
MINDWEFT_CODING_WORKSPACES=/path/to/repo1,/path/to/repo2
```

```bash
uv run mindweft-coding-workspace --workspace /path/to/repo1 --workspace /path/to/repo2
```

When trusted-local shell support is enabled, shell `cwd` values may be under any configured
workspace root and default to the first root.

### Workspace scopes

When a runner config exposes multiple workspace roots, you can define named scopes in
`mindweft.toml` and select one scope for the run. The MVP scope behavior is advisory: it
narrows the roots passed to the runner-generated MCP server commands and coding skill prompt,
but it is not a standalone security boundary for already-running external tools. Keep the
outer `coding.workspaces` list as the broad set of allowed roots; scope roots should sit
inside those configured roots.

```toml
[coding]
workspaces = ["/Users/example/code", "/Users/example/dotfiles"]
default_workspace_scope = "mindweft"

[coding.workspace_scopes.mindweft]
roots = ["/Users/example/code/mindweft"]
description = "Mindweft runtime and coding workspace development"

[coding.workspace_scopes.dotfiles]
roots = ["/Users/example/dotfiles"]
description = "Personal shell/editor configuration"
```

Resolution order is:

1. `--workspace-scope` or `MINDWEFT_CODING_WORKSPACE_SCOPE`;
2. the active default skill's `workspace_scope` / `workspaceScope`, when present in tenant config;
3. `coding.default_workspace_scope` / `MINDWEFT_CODING_DEFAULT_WORKSPACE_SCOPE`;
4. all configured workspace roots when no scope is selected.

Unknown scope names fail before the runner starts. When a scope is active, the generated
coding prompt includes `Active workspace scope: <name>` and tells the model to stay within
those roots unless the user explicitly asks to switch scope.

The runner starts the bridge with read-only filesystem tools by default. If you provide
`MINDWEFT_TENANT_EXECUTION_CONFIGS` with an `allowed_tools` list for the configured
`fs-workspace` server, the runner mirrors that list into the bridge's `--allowed-tool` filter
so fuller coding profiles can expose additional filesystem MCP tools. It also mirrors the
server `path_policy` into the bridge's `--deny-glob` and `--allow-glob` filters. You can
override the bridge path policy directly with comma-separated globs:

```dotenv
MINDWEFT_CODING_BRIDGE_DENY_GLOBS=**/.env*,**/.git/**,**/.venv/**,**/.pytest_cache/**,**/.ruff_cache/**,**/.uv-cache/**
MINDWEFT_CODING_BRIDGE_ALLOW_GLOBS=**/.env*.template,**/.env*.driver.sh
```

Direct bridge env vars take precedence over the mirrored tenant path policy.

### Declarative MCP server specs

For more than the built-in filesystem/text/shell tools, keep MCP server launch/connect
definitions directly in `minigent.toml` with `[[coding.mcp_server_specs]]`. This keeps the
unified config self-contained instead of pointing at a secondary MCP server file. Generated
server entries default to MCP `2026-07-28`: Mindweft's official SDK v2 client probes
`server/discover`, uses stateless per-request metadata when supported, and falls back to the
`2025-11-25` initialization/session flow for older servers. Mindweft applies tool and path
policy around the SDK client. The stdio bridge and shared gateway translate both HTTP-facing
forms through an SDK v2 client connected to each stdio subprocess, so modern requests do not
need bridge-issued `MCP-Session-Id` headers while legacy requests remain session-checked. The
bridge keeps multiple bounded legacy sessions valid concurrently, so one client's initialize
request does not invalidate another active client. Each server entry can define:

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
  When `MINDWEFT_CODING_MCP_GATEWAY_ENABLED=true`, stdio server entries do not need these
  fields; generated tenant URLs use the shared gateway path `/<prefix>/<server-name>`.
- `profiles`: capability profiles that should include this server, such as `inspect`, `edit`,
  or `test`.
- `allowed_tools` and `path_policy`: tool filters and bridge/tool path filters.
- `env`: extra process environment for stdio servers and managed HTTP servers.
- `headers`: HTTP headers to send when Mindweft calls the server URL.
- `managed`: for `transport: "http"`, start `command` as a child process before Mindweft.
  Defaults to `false`; unmanaged HTTP entries are only registered as external endpoints.
- `health_url`: optional URL to poll for a managed HTTP server before starting the API.
- `startup_timeout_seconds`: optional managed HTTP health-check timeout; defaults to `30`.
- `request_timeout`: optional stdio bridge/gateway timeout while waiting for one MCP response;
  defaults to `30`.
- `timeout_seconds`: optional Mindweft HTTP client timeout for calls to this MCP server;
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
MINDWEFT_CODING_MCP_GATEWAY_ENABLED=true
MINDWEFT_CODING_MCP_GATEWAY_PORT=8765
MINDWEFT_CODING_MCP_GATEWAY_PATH_PREFIX=/mcp
```

Start the runner as usual:

```bash
uv run mindweft-coding-workspace --env-file .env.coding
```

To export a restartable TOML for the full local coding stack, merge the API-owned config with
the locally resolved runner config:

```bash
uv run mindweft --env-file .env.coding config export --local-coding --output minigent.toml
# Equivalent coding-runner wrapper:
uv run mindweft-coding-workspace config export --env-file .env.coding --output minigent.toml
# Export without reading a coding dotenv file:
uv run mindweft-coding-workspace config export --no-env-file --output minigent.toml
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

When the runner generates `MINDWEFT_TENANT_EXECUTION_CONFIGS`, it derives
`tools.mcp_servers` and `capability_profiles.items[*].mcp_server_names` from the inline
`coding.mcp_server_specs`. If you provide `MINDWEFT_TENANT_EXECUTION_CONFIGS` yourself, the
inline specs still control process startup, but your explicit tenant config remains
authoritative for tool registration and profiles.

By default, each stdio server still runs behind its own local bridge/port for backwards
compatibility; if `port` is omitted, the runner assigns sequential local bridge ports. For new
multi-server setups, prefer a single local gateway process:

```dotenv
MINDWEFT_CODING_MCP_GATEWAY_ENABLED=true
MINDWEFT_CODING_MCP_GATEWAY_PORT=8765
MINDWEFT_CODING_MCP_GATEWAY_PATH_PREFIX=/mcp
```

With the gateway enabled, generated tenant config uses URLs shaped like:

```text
http://127.0.0.1:8765/mcp/fs-workspace
http://127.0.0.1:8765/mcp/text-workspace
http://127.0.0.1:8765/mcp/shell-workspace
```

If you provide `MINDWEFT_TENANT_EXECUTION_CONFIGS` yourself, update the `tools.mcp_servers`
URLs to the gateway paths; the runner does not rewrite explicit tenant config. In gateway
mode, per-server `host`, `port`, `path`, and `url` fields in the stdio server specs are legacy
compatibility settings and are not needed unless you also run without the gateway.

If an explicit tenant config references a gateway URL like `/mcp/text-workspace` but no matching
`[[coding.mcp_server_specs]]` entry or legacy MCP server-file entry was loaded, the runner prints
a warning before startup because those calls would otherwise fail with gateway 404 responses.

You can also run the gateway directly with a gateway config file:

```bash
uv run mindweft-mcp-stdio-gateway --config .data/mcp-gateway.json --port 8765
```

### Targeted text reads

The convenience runner can also start Mindweft's small targeted text-read MCP server. Its stdio
protocol transport and request validation use the official MCP Python SDK v2, while Mindweft's
workspace policy layer continues to enforce path containment and text/output limits. This server
complements the authoritative filesystem MCP by exposing efficient exact reads for known files
and regions:

- `read_text_file_lines(path, start_line, end_line)` reads an inclusive 1-based line range.
- `read_text_file_around(path, line, before, after)` reads context around a 1-based line.
- `search_text_file(path, pattern, before, after, max_matches)` searches within one text file
  and returns matching line contexts.

Enable it with `MINDWEFT_CODING_TEXT_ENABLED=true` or `--enable-text`. When the runner
generates the tenant config, it starts a second read-only MCP bridge named `text-workspace`
on port `8767` and adds it to the default `inspect` capability profile:

```bash
uv run mindweft-coding-workspace --env-file .env.coding --enable-text
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
MINDWEFT_CODING_TEXT_ENABLED=true
MINDWEFT_CODING_TEXT_BRIDGE_NAME=text-workspace
MINDWEFT_CODING_TEXT_BRIDGE_PORT=8767
```

If you provide `MINDWEFT_TENANT_EXECUTION_CONFIGS` yourself, include the text MCP server in
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

To enable trusted-local shell commands, set `MINDWEFT_CODING_SHELL_ENABLED=true` or pass
`--enable-shell`. When the runner generates the tenant config, this starts a second MCP bridge
named `shell-workspace` on port `8766` and adds a non-default `test` capability profile that
can call `shell-workspace.run_command`:

```bash
uv run mindweft-coding-workspace --env-file .env.coding --enable-shell
uv run python scripts/demo_client.py \
  --tenant-id demo-tenant \
  --capability-profile test \
  '/tool shell-workspace.run_command {"command":"uv run pytest","cwd":"/path/to/workspace"}'
```

The shell MCP server uses the official MCP Python SDK v2 for its stdio protocol transport and
request validation. Mindweft's shell policy layer still requires command working directories to
stay under one of the configured workspace roots, passes through only a small environment
allowlist, disables stdin, enforces a timeout, and truncates stdout/stderr. Keep
`[app].tool_timeout_seconds` greater than or equal to the shell MCP
`request_timeout`/`timeout_seconds`; `mindweft config doctor` warns when the outer runtime
timeout is shorter. Commands run through `/bin/sh` by default. If you define `shell-workspace`
explicitly in unified config and want zsh, configure the server command itself:

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
MINDWEFT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES=git,rg,find,ls,pwd,uv run pytest,uv run ruff check,uv run basedpyright
```

That blocks commands whose strings do not exactly match or start with one of those prefixes,
such as `cat .env`. Treat this as defense-in-depth, not a sandbox: broad prefixes such as
`git` or `python` can still have surprising effects, and shell syntax is flexible. Only enable
shell for trusted local workspaces or run the bridge/server inside a separate sandbox.

`.env.coding.template` also includes a commented Generic OAuth LLM example for coding profiles.
Uncomment it, fill in the OAuth/provider values, start the runner, then open
`http://127.0.0.1:8000/oauth/generic/open` to authorize the LLM provider. The trusted-local coding
runner first looks for its tenant-scoped credential and then falls back to the global credential
created by this login route. This fallback is enabled only by the coding-workspace runner for its
configured coding tenant; normal tenant execution remains strictly tenant-scoped.

## Smoke test

To smoke-test the flow without creating an env file, run the one-shot filesystem MCP demo
script. It starts the same bridge and a local Mindweft API process, creates an `inspect`
thread, then calls the filesystem MCP `list_directory` and `read_file` tools through
Mindweft's mock adapter:

```bash
uv run python scripts/demo_filesystem_mcp.py --workspace /path/to/workspace
```

Tenant catalog services can opt into bearer-token rotation through the tenant Tools editor,
even when custom MCP servers are disabled. This does not change workspace path policies,
shell permissions, or forwarded-identity services. See **Tenant MCP bearer credential rotation**
in [Reference](reference.md) for the catalog opt-in and tenant-owner API. Personal MCP
credentials remain a separate user-scoped configuration surface.

Tenant catalog cards also support explicit discovery-only connection tests. Reading connection
status does not contact MCP servers; last-check timestamps describe historical observations rather
than continuous health. These controls do not alter workspace scopes, path restrictions, or tool
allowlists. See **Inspecting and testing tenant catalog connections** in [Reference](reference.md).
