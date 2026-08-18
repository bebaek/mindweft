#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_NAMESPACE="${IMAGE_NAMESPACE:?set IMAGE_NAMESPACE to your GitHub user or org, or add IMAGE_NAMESPACE to .env}"
IMAGE_NAME="${IMAGE_NAME:-mindweft}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64}"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAMESPACE}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "Building and pushing ${FULL_IMAGE}"
docker buildx build \
  --platform "${PLATFORMS}" \
  --tag "${FULL_IMAGE}" \
  --push \
  "${ROOT_DIR}"
