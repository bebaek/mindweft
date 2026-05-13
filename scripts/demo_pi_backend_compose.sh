#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/compose.pi-backend-demo.yaml"
MINIGENT_PORT="${MINIGENT_PORT:-8000}"
KEEP_RUNNING=false
PREPARE_PI_HOME="${MINIGENT_PREPARE_PI_CONTAINER_HOME:-true}"
DEMO_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-running)
      KEEP_RUNNING=true
      shift
      ;;
    --no-prepare-pi-home)
      PREPARE_PI_HOME=false
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
  if [[ $status -ne 0 ]]; then
    docker compose -f "$COMPOSE_FILE" logs --tail=120 minigent local-agent-wrapper >&2 || true
  fi
  if [[ "$KEEP_RUNNING" != "true" ]]; then
    docker compose -f "$COMPOSE_FILE" down >/dev/null 2>&1 || true
  elif [[ $status -eq 0 ]]; then
    echo "compose stack is still running; stop it with:"
    echo "docker compose -f $COMPOSE_FILE down"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

wait_for_minigent() {
  local url="http://127.0.0.1:$MINIGENT_PORT/health"
  for _ in {1..120}; do
    if python3 - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request
urllib.request.urlopen(sys.argv[1], timeout=1).read()
PY
    then
      return 0
    fi
    sleep 0.5
  done
  echo "Minigent did not become healthy at $url" >&2
  return 1
}

cd "$ROOT_DIR"
mkdir -p .pi-container/agent

if [[ "$PREPARE_PI_HOME" == "true" && -d "${PI_HOST_AGENT_DIR:-${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}}" ]]; then
  ./scripts/prepare-pi-container-home.sh
fi

docker compose -f "$COMPOSE_FILE" up -d --build
wait_for_minigent

if [[ ${#DEMO_ARGS[@]} -gt 0 ]]; then
  uv run python scripts/demo_pi_mcp_broker.py \
    --base-url "http://127.0.0.1:$MINIGENT_PORT" \
    "${DEMO_ARGS[@]}"
else
  uv run python scripts/demo_pi_mcp_broker.py \
    --base-url "http://127.0.0.1:$MINIGENT_PORT"
fi
