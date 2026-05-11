#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_OPENCODE_DATA="${OPENCODE_DATA_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/opencode}"
SOURCE_OPENCODE_CONFIG="${OPENCODE_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}"
TARGET_OPENCODE_HOME="${OPENCODE_CONTAINER_HOME:-$ROOT_DIR/.opencode-container}"
TARGET_DATA="$TARGET_OPENCODE_HOME/data"
TARGET_CONFIG="$TARGET_OPENCODE_HOME/config"

if [[ ! -f "$SOURCE_OPENCODE_DATA/auth.json" ]]; then
  echo "missing OpenCode auth file: $SOURCE_OPENCODE_DATA/auth.json" >&2
  echo "Run 'opencode providers' locally first, or set OPENCODE_DATA_HOME to the directory containing auth.json." >&2
  exit 1
fi

mkdir -p "$TARGET_DATA" "$TARGET_CONFIG"
chmod 755 "$TARGET_OPENCODE_HOME"
chmod 777 "$TARGET_DATA" "$TARGET_CONFIG"

install -m 644 "$SOURCE_OPENCODE_DATA/auth.json" "$TARGET_DATA/auth.json"

if [[ -f "$SOURCE_OPENCODE_CONFIG/opencode.jsonc" ]]; then
  install -m 644 "$SOURCE_OPENCODE_CONFIG/opencode.jsonc" "$TARGET_CONFIG/opencode.jsonc"
elif [[ -f "$SOURCE_OPENCODE_CONFIG/opencode.json" ]]; then
  install -m 644 "$SOURCE_OPENCODE_CONFIG/opencode.json" "$TARGET_CONFIG/opencode.json"
fi

cat <<MSG
Prepared $TARGET_OPENCODE_HOME

This directory contains OpenCode credentials copied from $SOURCE_OPENCODE_DATA/auth.json.
It is ignored by git and intended only for this local Compose demo.
MSG
