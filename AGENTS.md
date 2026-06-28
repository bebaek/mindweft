# Agent Instructions

Read [README.md](README.md) before making changes. Follow project setup, command, config,
and workflow guidance from the README and linked docs.

## Safety

- Do not read or write secrets such as `.env`, `.env.*`, credentials, tokens, or private keys
  unless the user explicitly asks and the active tool policy allows it.
- Treat `minigent.toml` as local user configuration unless the user explicitly asks to edit it.
  Prefer updating `minigent.toml.template` and docs for committed config changes.
- Prefer git-tracked source files. Use `git status`, `git diff`, or `git ls-files` when needed
  to distinguish tracked, ignored, generated, and local-only files.

## Making changes

- Keep changes small and focused.
- Preserve existing public APIs, config names, and environment variable compatibility unless the
  user requests a breaking change.
- When adding or changing config parsing, maintain existing snake_case/camelCase compatibility
  where that pattern already exists.
- When changing user-facing behavior, setup, config, CLI usage, MCP behavior, or examples, update
  the relevant docs in the same change.
- Prefer updating committed examples/templates over ignored local config files.

## Validation

- Use `uv` commands described in the README/docs.
- Run targeted tests for touched behavior.
- Run Ruff formatting/checking for touched Python files.
- If validation cannot be run, say so clearly.

## MCP and coding-workspace changes

- Preserve path policy behavior and deny-glob defaults for sensitive files.
- Text workspace tools are read-only and should support all active workspace roots.
- Shell access is trusted-local only and should remain explicitly enabled/allowlisted.
- For MCP server specs, workspace scopes, filesystem/text/shell tools, or capability profile
  changes, update `docs/coding-workspace.md` and tests as appropriate.

## Final response checklist

When making code changes, summarize:

1. What changed.
2. What validation ran.
3. Any local-only or intentionally unstaged files.
4. A concise Conventional Commits message, if relevant.
