#!/usr/bin/env bash
set -euo pipefail

REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_NAMESPACE="${IMAGE_NAMESPACE:?set IMAGE_NAMESPACE to your GitHub user or org}"
IMAGE_NAME="${IMAGE_NAME:-minigent}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64}"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAMESPACE}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "Building and pushing ${FULL_IMAGE}"
docker buildx build \
  --platform "${PLATFORMS}" \
  --tag "${FULL_IMAGE}" \
  --push \
  .
