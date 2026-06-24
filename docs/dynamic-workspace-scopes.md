# Dynamic Workspace Narrowing by Skill

## Status

Planning note for an MVP. This document captures the discussed design for letting Minigent's coding workflow narrow the visible workspace based on the active skill or profile.

## Problem

The coding workspace runner can be configured with multiple workspace roots. That is useful for multi-repo work, but it also creates ambiguity and risk:

- The assistant may inspect or edit the wrong repository.
- Repository-specific instructions can be mixed together.
- A skill intended for one project may still see unrelated paths.
- Future MCP filesystem, shell, and code-navigation tools need a clear scope boundary.

The goal is to make workspace selection explicit and skill-aware while preserving the existing multi-root behavior when no scopes are configured.

## Desired Model

Use separate concepts for behavior, workspace access, and repo-local instructions:

```text
skill = behavior capability
profile = tool and execution permissions
workspace scope = allowed paths/repos
AGENTS.md / repo docs = scoped repository instructions
```

A skill should be able to request or default to a named workspace scope. The runtime should resolve that scope to one or more roots and use only those roots when building coding instructions and, later, MCP tool configuration.

## MVP

The MVP should be deliberately small and backwards compatible.

### Configuration

Add named coding workspace scopes to `minigent.toml`, for example:

```toml
[coding]
workspaces = ["/Users/burm/code", "/Users/burm/dotfiles"]
default_workspace_scope = "minigent"

[coding.workspace_scopes.minigent]
roots = ["/Users/burm/code/minigent"]
description = "Minigent runtime and coding workspace development"

[coding.workspace_scopes.dotfiles]
roots = ["/Users/burm/dotfiles"]
description = "Personal shell/editor configuration"
```

Open questions for implementation:

- Whether scope keys should be nested tables as above or an array of tables.
- Whether environment variables need a first-class scope syntax immediately, or whether TOML-only is enough for the MVP.
- Whether `default_workspace_scope` belongs under `[coding]` or under a skill/capability profile.

### Resolution Rules

1. If an explicit scope is selected for the coding run, use that scope.
2. Else if a skill declares a workspace scope, use the skill's scope.
3. Else if `[coding].default_workspace_scope` is set, use that scope.
4. Else use the existing configured workspace roots unchanged.

Invalid scope references should fail clearly before the run starts.

### Prompt Behavior

For the MVP, narrowing is prompt-level and runner-level, not a hard security boundary.

The generated coding skill prompt should list only the active scope roots as the current workspace. It should also state the scope name and make clear that work should stay within those roots unless the user asks to change scope.

Example prompt fragment:

```text
Active workspace scope: minigent
Workspace roots:
- /Users/burm/code/minigent

Stay within these roots for file inspection and edits unless the user explicitly asks to switch scope.
```

### Compatibility

Existing configs should continue to work:

- If no scopes are configured, use `MINIGENT_CODING_WORKSPACES` / `MINIGENT_CODING_WORKSPACE` and existing TOML `coding.workspaces` behavior.
- Multi-root workspaces remain supported.
- No existing skill is required to declare a scope.

## Follow-up: Runtime Enforcement

Prompt-only narrowing is useful, but it is not sufficient as a security or correctness boundary. Later iterations should use the resolved scope to configure the actual tool layer.

Potential enforcement points:

1. Filesystem MCP: expose only selected roots to read/write/list/search tools.
2. Shell MCP: run commands only with working directories inside the active roots.
3. Codebase-memory MCP: index/search only active projects or paths.
4. Browser or network tools: optionally couple scope to project-specific allowlists.
5. Peer-agent delegation: pass the active scope to delegated coding agents.

The long-term goal is that a selected scope becomes the source of truth for both instructions and tool permissions.

## Skill Integration

Skills can opt into scopes in a lightweight way. A future skill metadata shape could look like:

```yaml
---
name: minigent-coding
description: Work on the Minigent repository
workspace_scope: minigent
---
```

The runner can read that metadata when assembling the coding instructions. If a user explicitly selects another scope, the explicit user/runtime selection should win over the skill default.

## Risks and Mitigations

- **False sense of security:** Prompt narrowing is not hard enforcement. Document it as advisory until MCP narrowing is implemented.
- **Config complexity:** Keep the MVP to named scopes with roots and optional descriptions.
- **Path confusion:** Normalize and validate scope roots against configured/allowed workspace roots.
- **Repo instruction leakage:** Load repo-local instructions only from active scope roots where possible.
- **Unexpected breakage:** Preserve all-root fallback behavior when scopes are absent.

## Implementation Sketch

1. Add a `WorkspaceScope` config model with `name`, `roots`, and optional `description`.
2. Extend coding config parsing to include `workspace_scopes` and `default_workspace_scope`.
3. Add a resolver that returns active roots plus metadata for a requested/default scope.
4. Update coding prompt generation to use resolved active roots.
5. Add tests for:
   - no scopes configured preserves current roots;
   - default scope narrows roots;
   - explicit scope overrides default;
   - unknown scope fails clearly;
   - invalid paths are rejected or warned according to existing workspace validation behavior.
6. Update `docs/coding-workspace.md` and `docs/minigent-toml.md` after implementation.

## Acceptance Criteria

- A user can define at least one named workspace scope in `minigent.toml`.
- A default scope can narrow the coding runner's prompt roots.
- Existing configs without scopes behave exactly as before.
- Unknown scopes produce a clear configuration/runtime error.
- The documentation states that MVP scope narrowing is advisory until tool-level enforcement is added.
