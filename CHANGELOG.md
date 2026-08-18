# Changelog

All notable changes to Mindweft are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Mindweft has not yet
published a stable release, so the current version remains pre-release quality even though the
package version is `0.1.0`.

## [Unreleased]

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
