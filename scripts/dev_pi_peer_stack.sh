#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MINDWEFT_REPO="${MINDWEFT_REPO:-${MINIGENT_REPO:-$ROOT_DIR}}"
WRAPPER_DIR="$MINDWEFT_REPO/local-agent-wrapper"
MINDWEFT_PI_WORKSPACE="${MINDWEFT_PI_WORKSPACE:-${MINIGENT_PI_WORKSPACE:-$MINDWEFT_REPO}}"
AGENT_ALLOWED_WORKSPACES="${AGENT_ALLOWED_WORKSPACES:-$MINDWEFT_PI_WORKSPACE}"
AGENT_PI_TOOLS="${AGENT_PI_TOOLS:-read,grep,find,ls,write,edit,bash}"
MINDWEFT_HOST="${MINDWEFT_HOST:-${MINIGENT_HOST:-127.0.0.1}}"
MINDWEFT_PORT="${MINDWEFT_PORT:-${MINIGENT_PORT:-8000}}"
MINDWEFT_PI_WRAPPER_PORT="${MINDWEFT_PI_WRAPPER_PORT:-${MINIGENT_PI_WRAPPER_PORT:-8010}}"
MINDWEFT_PI_PEER_NAME="${MINDWEFT_PI_PEER_NAME:-${MINIGENT_PI_PEER_NAME:-pi}}"
MINDWEFT_MCP_BROKER_ENABLED="${MINDWEFT_MCP_BROKER_ENABLED:-${MINIGENT_MCP_BROKER_ENABLED:-true}}"
AGENT_RUNTIME="${AGENT_RUNTIME:-pi}"
AGENT_COMMAND="${AGENT_COMMAND:-pi}"

WRAPPER_PID=""
MINDWEFT_PID=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM

  if [[ -n "$MINDWEFT_PID" ]] && kill -0 "$MINDWEFT_PID" 2>/dev/null; then
    kill "$MINDWEFT_PID" 2>/dev/null || true
    wait "$MINDWEFT_PID" 2>/dev/null || true
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
  echo "Set MINDWEFT_REPO to the Mindweft checkout path." >&2
  exit 1
fi

(
  cd "$WRAPPER_DIR"
  AGENT_RUNTIME="$AGENT_RUNTIME" \
    AGENT_COMMAND="$AGENT_COMMAND" \
    AGENT_ALLOWED_WORKSPACES="$AGENT_ALLOWED_WORKSPACES" \
    AGENT_PI_TOOLS="$AGENT_PI_TOOLS" \
    uv run uvicorn local_agent_wrapper.app:app \
    --host "$MINDWEFT_HOST" \
    --port "$MINDWEFT_PI_WRAPPER_PORT"
) &
WRAPPER_PID=$!

(
  cd "$MINDWEFT_REPO"
  MINDWEFT_AUTH_MODE=dev-headers \
    MINDWEFT_PEER_AGENTS="[{\"name\":\"$MINDWEFT_PI_PEER_NAME\",\"base_url\":\"http://$MINDWEFT_HOST:$MINDWEFT_PI_WRAPPER_PORT\"}]" \
    MINDWEFT_AGENT_BACKEND=peer_agent \
    MINDWEFT_AGENT_BACKEND_PEER="$MINDWEFT_PI_PEER_NAME" \
    MINDWEFT_AGENT_BACKEND_CWD="$MINDWEFT_PI_WORKSPACE" \
    MINDWEFT_MCP_BROKER_BASE_URL="http://$MINDWEFT_HOST:$MINDWEFT_PORT" \
    MINDWEFT_MCP_BROKER_ENABLED="$MINDWEFT_MCP_BROKER_ENABLED" \
    uv run uvicorn app.main:app \
    --host "$MINDWEFT_HOST" \
    --port "$MINDWEFT_PORT"
) &
MINDWEFT_PID=$!

echo "Pi wrapper: http://$MINDWEFT_HOST:$MINDWEFT_PI_WRAPPER_PORT"
echo "Mindweft:   http://$MINDWEFT_HOST:$MINDWEFT_PORT"
echo "Workspace:  $MINDWEFT_PI_WORKSPACE"
echo "Allowed:    $AGENT_ALLOWED_WORKSPACES"
echo "Pi tools:   $AGENT_PI_TOOLS"
echo "Web UI:     http://$MINDWEFT_HOST:$MINDWEFT_PORT/web/"
echo "Press Ctrl-C to stop both."

wait "$WRAPPER_PID" "$MINDWEFT_PID"
