#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running sms-relay release.

set -euo pipefail

RELEASE="${RELEASE:-sms-relay}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"

# Materialize values.local.yaml from 1Password when it's missing and a template
# exists. Convenience only: values.local.yaml is still the contract, so this
# no-ops without `op` — e.g. in the claude-workspace pod, which is fed by
# `scripts/secrets.sh publish` instead. See values.local.tpl.yaml.
if [[ ! -f "$LOCAL_VALUES" && -f "${HERE}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
  echo "==> materializing values.local.yaml from 1Password"
  op inject -i "${HERE}/values.local.tpl.yaml" -o "$LOCAL_VALUES" \
    || { echo "FAIL: op inject failed. Signed in?  eval \$(op signin)"; exit 1; }
  chmod 600 "$LOCAL_VALUES"
fi

VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# Refuse to roll onto a tag that isn't in the registry. The Deployment uses
# strategy: Recreate (the data PVC is RWO), so the running pod is torn down
# *before* the replacement pulls — pointing at a missing tag takes the relay
# down rather than failing safe, and inbound SMS delivered while it is down are
# simply lost. Checking first turns that outage into an error.
REPO="$(awk '/^  repository:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "$VALUES")"
TAG="$(awk '/^  tag:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "$VALUES")"
if [[ "$REPO" == ghcr.io/* && -n "$TAG" ]] && command -v curl >/dev/null && command -v python3 >/dev/null; then
  echo "==> Verifying ${REPO}:${TAG} exists"
  IMG_PATH="${REPO#ghcr.io/}"
  # If the package is private, an anonymous pull token is refused; reuse the
  # GHCR PAT that build.sh already needs. Without auth every request 301s and
  # the check can't tell a real tag from a typo.
  BASIC="$(python3 - "$HOME/.docker/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print(json.load(fh).get("auths", {}).get("ghcr.io", {}).get("auth", ""))
except Exception:
    print("")
PY
)"
  TOKEN="$(curl -fsSL ${BASIC:+-H "Authorization: Basic ${BASIC}"} \
           "https://ghcr.io/token?scope=repository:${IMG_PATH}:pull&service=ghcr.io" \
           | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)"
  if [[ -z "$TOKEN" ]]; then
    echo "    skipped (no GHCR credentials — cannot verify)" >&2
  else
    CODE="$(curl -sL -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" \
            -H 'Accept: application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
            "https://ghcr.io/v2/${IMG_PATH}/manifests/${TAG}" || echo 000)"
    if [[ "$CODE" != "200" ]]; then
      echo "ERROR: ${REPO}:${TAG} is not in the registry (HTTP ${CODE})." >&2
      echo "       Run build.sh first — deploying now would take the relay down." >&2
      exit 1
    fi
    echo "    ok"
  fi
fi

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
