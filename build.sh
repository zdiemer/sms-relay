#!/usr/bin/env bash
# Build the sms-relay image and push it to the in-cluster registry (infra/registry) (same pattern as the other
# self-written apps in the selfhosted repo). The registry rather than
# side-loading into containerd, so every node can pull it.
#
# Re-run whenever you edit the Dockerfile / app, then run upgrade.sh (the static
# tag + pullPolicy: Always means a pod restart picks up the new image).
#
# Requires: docker login registry.zachd.duckdns.org, OR — inside the
# claude-workspace pod where there is no docker — buildctl + the in-cluster
# buildkitd (infra/buildkit) + the registry credential in ~/.docker/config.json.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk '/^  repository:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "${HERE}/values.yaml")"
TAG="$(awk '/^  tag:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

if command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${HERE}"
  echo "==> Pushing ${IMAGE}"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — add the registry credential first (selfhosted/infra/registry/README.md)"; exit 1; }
  echo "==> Building + pushing ${IMAGE} (buildctl → ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${HERE}" \
    --local dockerfile="${HERE}" \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done. Run upgrade.sh (or delete the pod) to roll onto the new image."
