#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${MINDWEFT_VOICE_ENV_FILE:-${MINIGENT_VOICE_ENV_FILE:-.env.voice}}"
BACKEND="${MINDWEFT_VOICE_BACKEND:-${MINIGENT_VOICE_BACKEND:-passive-audio}}"

PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
  UV_TOOL_BIN="$(uv tool dir --bin 2>/dev/null || true)"
  if [[ -n "$UV_TOOL_BIN" ]]; then
    PATH="$UV_TOOL_BIN:$PATH"
  fi
fi
export PATH

usage() {
  cat <<'USAGE'
Usage: scripts/run-client-linux.sh [--env-file PATH] [--backend BACKEND] [client args...]

Loads a client env file, then runs mindweft-client.

Environment overrides:
  MINDWEFT_VOICE_ENV_FILE   Env file path. Default: .env.voice
  MINDWEFT_VOICE_BACKEND    Client backend. Default: passive-audio
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:?missing value for --env-file}"
      shift 2
      ;;
    --backend)
      BACKEND="${2:?missing value for --backend}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  echo "Create it first or pass --env-file PATH." >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

exec mindweft-client "$BACKEND" "$@"
