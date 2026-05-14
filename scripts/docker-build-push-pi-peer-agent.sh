#!/usr/bin/env bash
set -euo pipefail

REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_NAMESPACE="${IMAGE_NAMESPACE:?set IMAGE_NAMESPACE to your GitHub user or org}"
IMAGE_NAME="${IMAGE_NAME:-minigent-local-agent-wrapper}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64}"
INSTALL_PI="${INSTALL_PI:-true}"
INSTALL_CODEX="${INSTALL_CODEX:-false}"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAMESPACE}/${IMAGE_NAME}:${IMAGE_TAG}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! docker buildx version >/dev/null 2>&1; then
  echo "docker buildx is required" >&2
  exit 1
fi

echo "Building and pushing ${FULL_IMAGE}"
docker buildx build \
  --platform "${PLATFORMS}" \
  --build-arg "INSTALL_PI=${INSTALL_PI}" \
  --build-arg "INSTALL_CODEX=${INSTALL_CODEX}" \
  --tag "${FULL_IMAGE}" \
  --push \
  "${ROOT_DIR}/local-agent-wrapper"
