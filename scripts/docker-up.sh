#!/usr/bin/env bash
set -euo pipefail

MINIGENT_ENV_FILE=.env.docker docker compose --env-file .env.docker up -d --force-recreate "$@"
