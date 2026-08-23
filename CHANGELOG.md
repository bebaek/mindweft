# Changelog

All notable changes to Mindweft are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Mindweft has not yet
published a stable release, so the current version remains pre-release quality even though the
package version is `0.1.0`.

## [Unreleased]

### Added

- Conversation libraries now support tenant-scoped title search, persistent pinning and archiving,
  pinned-first browser sections, an archived browser view, and matching one-shot and interactive
  CLI controls without changing related branches.
- Thread lineage can now be retrieved through a tenant-scoped API and navigated persistently in the
  browser through parent, sibling, and child controls; interactive CLI chat adds `/lineage`,
  `/parent`, and `/children [number]` without requiring thread IDs.
- Browser messages now provide a **Branch from here** action, and interactive CLI branching supports
  the latest visible message, numbered recent-message selection, an interactive picker, and an
  advanced explicit message-ID option.
- Manual thread compaction now creates a summarized child fork and preserves the complete source
  thread; CLI and browser clients continue on the returned child automatically.

### Fixed

- The browser **Branch from here** action now uses a high-contrast foreground and background on hover
  and keyboard focus.
- Thread forks now copy unexpired private values referenced by inherited context into the child
  within the authenticated user's scope, without cloning pending consent actions or extending value
  expiry.
- The production console now renders structured streaming run errors, including provider
  authentication failures, as user-facing messages instead of crashing the conversation view.

### Changed

- The browser now derives its core light and dark theme tokens from curated Radix Sage, Green, Lime,
  Amber, and Red scales; semantic application tokens replace component-specific theme color pairs
  for interactive, positive, warning, focus, message, and error states.
- Opt-in automatic compaction now advances a model-visible summary boundary without deleting raw
  messages or attachments; direct `AgentRuntime` construction also defaults automatic compaction to
  disabled, matching server configuration.
- Principal-scoped built-in user MCP tools now use the canonical `mindweft_user_mcp.*` prefix
  instead of the legacy `minigent_user_mcp.*` prefix. Existing `shared:minigent-user-mcp` profile
  references remain accepted for stored-configuration compatibility.

## [0.1.0] - 2026-08-18

### Added

- Canonical `mindweft` Python distribution metadata.
- Apache License 2.0 licensing for the source and built distributions.
- Canonical `mindweft`, `mindweft-client`, coding-workspace, and MCP console commands.
- Canonical `mindweft_client`, `mindweft_config`, `mindweft_mcp`, and `mindweft_workspace`
  implementation namespaces.
- Canonical `MINDWEFT_*` environment variables, configuration paths, deployment identities, and
  MCP identities.
- Compatibility aliases for existing Minigent commands, imports, configuration, and state paths.

### Changed

- User-facing product naming now uses Mindweft.
- The canonical source repository is `https://github.com/bebaek/mindweft`.
- Mindweft names take precedence when both canonical and legacy configuration values are present.
- New state and configuration defaults use Mindweft paths while preserving discovery of existing
  Minigent data.
- The installable wheel and source distribution are named `mindweft`.

### Compatibility

- Legacy `minigent_*` imports resolve to the same module objects as their canonical `mindweft_*`
  counterparts.
- Legacy `minigent-*` console commands remain installed by the `mindweft` distribution.
- Existing `MINIGENT_*` environment variables and `minigent.toml` files remain accepted as
  lower-precedence fallbacks.

See [Migrating from Minigent](docs/migrating-from-minigent.md) for upgrade guidance and the
compatibility policy.
