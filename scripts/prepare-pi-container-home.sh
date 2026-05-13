#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_PI_HOME="${PI_HOST_AGENT_DIR:-${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}}"
TARGET_PI_HOME="${PI_CONTAINER_AGENT_DIR:-$ROOT_DIR/.pi-container/agent}"

if [[ ! -d "$SOURCE_PI_HOME" ]]; then
  echo "missing Pi agent directory: $SOURCE_PI_HOME" >&2
  echo "Run 'pi' and log in locally first, or set PI_HOST_AGENT_DIR to your host Pi agent directory." >&2
  exit 1
fi

if [[ ! -f "$SOURCE_PI_HOME/auth.json" ]]; then
  echo "warning: $SOURCE_PI_HOME/auth.json was not found; copying Pi config anyway." >&2
fi

mkdir -p "$TARGET_PI_HOME"
cp -R "$SOURCE_PI_HOME/." "$TARGET_PI_HOME/"

# The wrapper runs as a non-root 'agent' user in the container. These permissions make
# the copied credentials readable/writable through the bind mount for local demo use.
find "$TARGET_PI_HOME" -type d -exec chmod 755 {} +
find "$TARGET_PI_HOME" -type f -exec chmod 644 {} +

cat <<MSG
Prepared $TARGET_PI_HOME

This directory contains Pi credentials/config copied from $SOURCE_PI_HOME.
It is ignored by git and intended only for this local Compose demo.
MSG
