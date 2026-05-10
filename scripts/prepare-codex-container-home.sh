#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
TARGET_CODEX_HOME="${CODEX_CONTAINER_HOME:-$ROOT_DIR/.codex-container}"

if [[ ! -f "$SOURCE_CODEX_HOME/auth.json" ]]; then
  echo "missing Codex auth file: $SOURCE_CODEX_HOME/auth.json" >&2
  echo "run 'codex login' on the host first" >&2
  exit 1
fi

mkdir -p "$TARGET_CODEX_HOME"
chmod 755 "$TARGET_CODEX_HOME"
install -m 644 "$SOURCE_CODEX_HOME/auth.json" "$TARGET_CODEX_HOME/auth.json"
chmod 644 "$TARGET_CODEX_HOME/auth.json"

if [[ -f "$SOURCE_CODEX_HOME/config.toml" ]]; then
  install -m 644 "$SOURCE_CODEX_HOME/config.toml" "$TARGET_CODEX_HOME/config.toml"
  chmod 644 "$TARGET_CODEX_HOME/config.toml"
else
  : >"$TARGET_CODEX_HOME/config.toml"
  chmod 644 "$TARGET_CODEX_HOME/config.toml"
fi

cat >&2 <<EOF
Prepared $TARGET_CODEX_HOME

This directory contains Codex credentials copied from $SOURCE_CODEX_HOME/auth.json.
Files are readable by the non-root demo container user. Keep the directory local, do not
commit it, and mount it only into trusted local demo containers.
EOF
