#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MINIGENT_REPO="${MINIGENT_REPO:-$ROOT_DIR}"
WRAPPER_DIR="$MINIGENT_REPO/local-agent-wrapper"
MINIGENT_PI_WORKSPACE="${MINIGENT_PI_WORKSPACE:-$MINIGENT_REPO}"
AGENT_ALLOWED_WORKSPACES="${AGENT_ALLOWED_WORKSPACES:-$MINIGENT_PI_WORKSPACE}"
MINIGENT_HOST="${MINIGENT_HOST:-127.0.0.1}"
MINIGENT_PORT="${MINIGENT_PORT:-8000}"
MINIGENT_PI_WRAPPER_PORT="${MINIGENT_PI_WRAPPER_PORT:-8010}"
MINIGENT_PI_PEER_NAME="${MINIGENT_PI_PEER_NAME:-pi}"
MINIGENT_MCP_BROKER_ENABLED="${MINIGENT_MCP_BROKER_ENABLED:-true}"
AGENT_RUNTIME="${AGENT_RUNTIME:-pi}"
AGENT_COMMAND="${AGENT_COMMAND:-pi}"

WRAPPER_PID=""
MINIGENT_PID=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM

  if [[ -n "$MINIGENT_PID" ]] && kill -0 "$MINIGENT_PID" 2>/dev/null; then
    kill "$MINIGENT_PID" 2>/dev/null || true
    wait "$MINIGENT_PID" 2>/dev/null || true
  fi
  if [[ -n "$WRAPPER_PID" ]] && kill -0 "$WRAPPER_PID" 2>/dev/null; then
    kill "$WRAPPER_PID" 2>/dev/null || true
    wait "$WRAPPER_PID" 2>/dev/null || true
  fi

  exit "$status"
}
trap cleanup EXIT INT TERM

if [[ ! -d "$WRAPPER_DIR" ]]; then
  echo "local-agent-wrapper not found at $WRAPPER_DIR" >&2
  echo "Set MINIGENT_REPO to the Minigent checkout path." >&2
  exit 1
fi

(
  cd "$WRAPPER_DIR"
  AGENT_RUNTIME="$AGENT_RUNTIME" \
  AGENT_COMMAND="$AGENT_COMMAND" \
  AGENT_ALLOWED_WORKSPACES="$AGENT_ALLOWED_WORKSPACES" \
    uv run uvicorn local_agent_wrapper.app:app \
      --host "$MINIGENT_HOST" \
      --port "$MINIGENT_PI_WRAPPER_PORT"
) &
WRAPPER_PID=$!

(
  cd "$MINIGENT_REPO"
  MINIGENT_AUTH_MODE=dev-headers \
  MINIGENT_PEER_AGENTS="[{\"name\":\"$MINIGENT_PI_PEER_NAME\",\"base_url\":\"http://$MINIGENT_HOST:$MINIGENT_PI_WRAPPER_PORT\"}]" \
  MINIGENT_AGENT_BACKEND=peer_agent \
  MINIGENT_AGENT_BACKEND_PEER="$MINIGENT_PI_PEER_NAME" \
  MINIGENT_AGENT_BACKEND_CWD="$MINIGENT_PI_WORKSPACE" \
  MINIGENT_MCP_BROKER_BASE_URL="http://$MINIGENT_HOST:$MINIGENT_PORT" \
  MINIGENT_MCP_BROKER_ENABLED="$MINIGENT_MCP_BROKER_ENABLED" \
    uv run uvicorn app.main:app \
      --reload \
      --host "$MINIGENT_HOST" \
      --port "$MINIGENT_PORT"
) &
MINIGENT_PID=$!

echo "Pi wrapper: http://$MINIGENT_HOST:$MINIGENT_PI_WRAPPER_PORT"
echo "Minigent:   http://$MINIGENT_HOST:$MINIGENT_PORT"
echo "Workspace:  $MINIGENT_PI_WORKSPACE"
echo "Allowed:    $AGENT_ALLOWED_WORKSPACES"
echo "Web UI:     http://$MINIGENT_HOST:$MINIGENT_PORT/web/"
echo "Press Ctrl-C to stop both."

wait "$WRAPPER_PID" "$MINIGENT_PID"
