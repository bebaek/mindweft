# Migrating from legacy Minigent

Mindweft is the canonical product and distribution name. The migration is compatibility-first:
existing commands, Python imports, environment variables, configuration files, and state paths
continue to work while new integrations use Mindweft names.

## Installing and upgrading

Once a release has been published, install the canonical `mindweft` distribution:

```bash
uv tool install mindweft
```

For a checkout of this repository:

```bash
uv sync --dev
```

The distribution continues to install the legacy command aliases, but it does not install a
second distribution named `minigent`. Dependency declarations and package installation commands
should therefore move to `mindweft`.

## Command names

Use the canonical commands for new automation:

| Canonical | Compatibility alias |
| --- | --- |
| `mindweft` | `minigent` |
| `mindweft-client` | `minigent-client` |
| `mindweft-coding-workspace` | `minigent-coding-workspace` |
| `mindweft-mcp-stdio-bridge` | `minigent-mcp-stdio-bridge` |
| `mindweft-mcp-stdio-gateway` | `minigent-mcp-stdio-gateway` |
| `mindweft-shell-mcp` | `minigent-shell-mcp` |
| `mindweft-text-mcp` | `minigent-text-mcp` |

The aliases currently have the same behavior and load the same implementation functions.

## Python imports

New code should import canonical namespaces:

```python
from mindweft_client.api_client import MindweftAPIClient
from mindweft_client.errors import MindweftAPIError
from mindweft_config.unified_config import resolve_unified_config
from mindweft_mcp.protocol import DEFAULT_MCP_PROTOCOL_VERSION
from mindweft_workspace.scopes import WorkspaceScope
```

The corresponding `minigent_*` modules remain compatibility aliases. A canonical module and its
legacy import resolve to the same module object, preventing duplicate class identities and split
module state.

Canonical public class names use `Mindweft*`. Existing legacy `Minigent*` class names remain
aliases where they were previously public.

## Environment variables

Prefer `MINDWEFT_*` variables. When a setting has both names, Mindweft reads the canonical value
first and uses the corresponding `MINIGENT_*` value only as a compatibility fallback.

For a safe incremental migration:

1. Add the `MINDWEFT_*` variable with the same value as the existing variable.
2. Restart and run `mindweft config doctor`.
3. Remove the old variable after confirming the resolved configuration.

Do not keep conflicting canonical and legacy values: the canonical value wins.

## Configuration files

Use `mindweft.toml` for new configuration. Discovery checks canonical locations before legacy
locations, including:

1. `./mindweft.toml`
2. `./minigent.toml`
3. the Mindweft user configuration path
4. the legacy Minigent user configuration path

Mindweft does not automatically rewrite or delete an existing `minigent.toml`. Copy or rename it
only during a controlled restart, then verify the result with:

```bash
mindweft config print --resolved
mindweft config doctor
```

## State and data paths

New defaults use Mindweft directories. Existing legacy Minigent state remains discoverable so an
upgrade does not silently create empty replacement databases or move live SQLite files.

Move durable state only while all Mindweft processes are stopped. Back up SQLite databases and
attachments before changing paths. Explicitly configured paths are not renamed automatically.

## Deployment and MCP identities

Canonical deployment settings, image names, development headers, MCP client information, and
administrative tool names use Mindweft. Compatibility inputs remain accepted where documented,
but external allowlists and integrations should be updated to recognize the canonical identities.

Review these areas before rollout:

- container image references and service names
- reverse-proxy headers and authentication policy
- MCP tool allowlists
- observability dashboards and log queries
- scripts that match command names or environment prefixes

## Compatibility policy

No removal version is scheduled for the legacy import packages, console commands, environment
variables, configuration discovery paths, or state fallbacks. Their eventual removal requires a
separately announced deprecation period and a major-version compatibility review.

The old distribution name is the exception: releases are published as `mindweft`, so package
manager dependencies must use the canonical distribution name.
