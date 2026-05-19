#!/usr/bin/env bash
set -euo pipefail

set -a
source .env.voice.docker
set +a

uv run minigent-client passive-audio "$@"
