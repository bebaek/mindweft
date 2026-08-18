#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/compose.peer-demo.yaml"
MINDWEFT_PORT="${MINDWEFT_PORT:-${MINIGENT_PORT:-8000}}"
PROMPT="Reply exactly: compose-opencode-ok"
KEEP_RUNNING=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-running)
      KEEP_RUNNING=true
      shift
      ;;
    --prompt)
      if [[ $# -lt 2 ]]; then
        echo "--prompt requires a value" >&2
        exit 2
      fi
      PROMPT="$2"
      shift 2
      ;;
    *)
      PROMPT="$1"
      shift
      ;;
  esac
done

cleanup() {
  local status=$?
  if [[ "$KEEP_RUNNING" != "true" ]]; then
    docker compose -f "$COMPOSE_FILE" down >/dev/null 2>&1 || true
  elif [[ $status -eq 0 ]]; then
    echo "compose stack is still running; stop it with:"
    echo "docker compose -f $COMPOSE_FILE down"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"

./scripts/prepare-opencode-container-home.sh

docker compose -f "$COMPOSE_FILE" up -d --build

uv run python scripts/check_peer_agent_demo.py \
  --mindweft-port "$MINDWEFT_PORT" \
  --peer-name opencode \
  --skip-wrapper-health \
  --check-running

uv run python scripts/demo_peer_agent_tool.py \
  --base-url "http://127.0.0.1:$MINDWEFT_PORT" \
  --peer opencode \
  --cwd /workspace/mindweft \
  --prompt "$PROMPT"
