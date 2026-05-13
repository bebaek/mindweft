#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/compose.pi-backend-demo.yaml"
MINIGENT_PORT="${MINIGENT_PORT:-8000}"
KEEP_RUNNING=false
DEMO_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-running)
      KEEP_RUNNING=true
      shift
      ;;
    --)
      shift
      DEMO_ARGS+=("$@")
      break
      ;;
    *)
      DEMO_ARGS+=("$1")
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
mkdir -p .pi-container/agent

docker compose -f "$COMPOSE_FILE" up -d --build

uv run python scripts/demo_pi_mcp_broker.py \
  --base-url "http://127.0.0.1:$MINIGENT_PORT" \
  "${DEMO_ARGS[@]}"
