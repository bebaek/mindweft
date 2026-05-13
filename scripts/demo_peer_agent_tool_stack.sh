#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER_DIR="$ROOT_DIR/local-agent-wrapper"
WORKSPACE="${AGENT_ALLOWED_WORKSPACES:-$ROOT_DIR}"
AGENT_RUNTIME="${AGENT_RUNTIME:-pi}"
AGENT_RUNTIME_NORMALIZED="$(printf '%s' "$AGENT_RUNTIME" | tr '[:upper:]' '[:lower:]')"
if [[ -z "${AGENT_COMMAND:-}" ]]; then
  case "$AGENT_RUNTIME_NORMALIZED" in
    codex) AGENT_COMMAND="codex" ;;
    pi) AGENT_COMMAND="pi" ;;
    *) AGENT_COMMAND="opencode" ;;
  esac
fi
AGENT_HOST="${AGENT_HOST:-127.0.0.1}"
AGENT_PORT="${AGENT_PORT:-8010}"
PEER_NAME="${MINIGENT_DEMO_PEER_NAME:-$AGENT_RUNTIME_NORMALIZED}"
MINIGENT_HOST="${MINIGENT_HOST:-127.0.0.1}"
MINIGENT_PORT="${MINIGENT_PORT:-8000}"
PROMPT="${1:-Summarize this repository in one paragraph. Do not edit files.}"
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/minigent-peer-tool-demo.XXXXXX")"

WRAPPER_PID=""
MINIGENT_PID=""

cleanup() {
  local status=$?
  if [[ -n "$MINIGENT_PID" ]] && kill -0 "$MINIGENT_PID" 2>/dev/null; then
    kill "$MINIGENT_PID" 2>/dev/null || true
    wait "$MINIGENT_PID" 2>/dev/null || true
  fi
  if [[ -n "$WRAPPER_PID" ]] && kill -0 "$WRAPPER_PID" 2>/dev/null; then
    kill "$WRAPPER_PID" 2>/dev/null || true
    wait "$WRAPPER_PID" 2>/dev/null || true
  fi
  if [[ $status -ne 0 ]]; then
    echo
    echo "demo failed; logs are in $LOG_DIR" >&2
    echo "agent wrapper log: $LOG_DIR/agent-wrapper.log" >&2
    echo "minigent log: $LOG_DIR/minigent.log" >&2
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

wait_for_url() {
  local label="$1"
  local url="$2"
  local attempts="${3:-60}"
  for _ in $(seq 1 "$attempts"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "$label did not become ready at $url" >&2
  return 1
}

echo "logs: $LOG_DIR"

uv run python scripts/check_peer_agent_demo.py \
  --workspace "$WORKSPACE" \
  --agent-runtime "$AGENT_RUNTIME" \
  --agent-command "$AGENT_COMMAND" \
  --agent-host "$AGENT_HOST" \
  --agent-port "$AGENT_PORT" \
  --peer-name "$PEER_NAME" \
  --minigent-host "$MINIGENT_HOST" \
  --minigent-port "$MINIGENT_PORT"

cd "$WRAPPER_DIR"
AGENT_ALLOWED_WORKSPACES="$WORKSPACE" \
AGENT_RUNTIME="$AGENT_RUNTIME" \
AGENT_COMMAND="$AGENT_COMMAND" \
  uv run uvicorn local_agent_wrapper.app:app \
    --host "$AGENT_HOST" \
    --port "$AGENT_PORT" \
    >"$LOG_DIR/agent-wrapper.log" 2>&1 &
WRAPPER_PID=$!

wait_for_url "agent wrapper" "http://$AGENT_HOST:$AGENT_PORT/health"

cd "$ROOT_DIR"
MINIGENT_ENABLE_PEER_AGENT_TOOL=true \
MINIGENT_PEER_AGENTS="[{\"name\":\"$PEER_NAME\",\"base_url\":\"http://$AGENT_HOST:$AGENT_PORT\",\"description\":\"Local $AGENT_RUNTIME wrapper\",\"capabilities\":[\"repository analysis\",\"codebase inspection\",\"read-only command execution in the allowed workspace\"],\"side_effects\":[\"runs $AGENT_RUNTIME CLI commands on the local host\"],\"version\":\"0.1.0\"}]" \
  uv run uvicorn app.main:app \
    --host "$MINIGENT_HOST" \
    --port "$MINIGENT_PORT" \
    >"$LOG_DIR/minigent.log" 2>&1 &
MINIGENT_PID=$!

wait_for_url "minigent" "http://$MINIGENT_HOST:$MINIGENT_PORT/health"

uv run python scripts/demo_peer_agent_tool.py \
  --base-url "http://$MINIGENT_HOST:$MINIGENT_PORT" \
  --peer "$PEER_NAME" \
  --cwd "$WORKSPACE" \
  --prompt "$PROMPT"
