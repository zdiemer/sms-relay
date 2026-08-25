#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running sms-relay release.

set -euo pipefail

RELEASE="${RELEASE:-sms-relay}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# Resolve the secret from 1Password into RAM for the life of this run. It is
# never written to a persistent disk, and it is removed on exit.
#
# $SELFHOSTED_LOCAL_VALUES lets a caller supply a path instead. The on-disk
# values.local.yaml is the last resort, for a clone that predates this.
resolve_local_values() {
  local here="$1" rt="" d
  if [[ -n "${SELFHOSTED_LOCAL_VALUES:-}" ]]; then printf '%s\n' "$SELFHOSTED_LOCAL_VALUES"; return 0; fi
  if [[ -f "${here}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
    # A tmpfs, asserted rather than assumed: /tmp is ext4 on some of these hosts,
    # so falling back to it would quietly reintroduce the file this removes.
    for d in "${XDG_RUNTIME_DIR:-}" "/run/user/$(id -u)" /dev/shm; do
      [[ -n "$d" && -d "$d" && -w "$d" ]] || continue
      case "$(stat -f -c %T "$d" 2>/dev/null)" in tmpfs|ramfs) rt="$d"; break ;; esac
    done
    [[ -n "$rt" ]] || { echo "FAIL: no tmpfs available; refusing to write the secret to a disk" >&2; return 1; }
    local f; f="$(mktemp "${rt}/values.local.XXXXXX")" || return 1
    chmod 600 "$f"
    op inject -i "${here}/values.local.tpl.yaml" -o "$f" -f >/dev/null 2>&1 \
      || { rm -f "$f"; echo "FAIL: op inject failed. Signed in?  eval \$(op signin)" >&2; return 1; }
    printf '%s\n' "$f"; return 0
  fi
  printf '%s\n' "${here}/values.local.yaml"
}
LOCAL_VALUES="$(resolve_local_values "$HERE")" || exit 1
[[ "$LOCAL_VALUES" == "${HERE}/"* ]] || trap 'rm -f "$LOCAL_VALUES"' EXIT INT TERM

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
if [[ "$REPO" == registry.zachd.duckdns.org/* && -n "$TAG" ]] && command -v curl >/dev/null && command -v python3 >/dev/null; then
  echo "==> Verifying ${REPO}:${TAG} exists"
  REG_HOST="${REPO%%/*}"
  IMG_PATH="${REPO#*/}"
  # The in-cluster registry (selfhosted/infra/registry): plain basic auth on
  # every request, with the credential build.sh already has in the docker
  # config. No token dance.
  BASIC="$(python3 - "$HOME/.docker/config.json" "$REG_HOST" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print(json.load(fh).get("auths", {}).get(sys.argv[2], {}).get("auth", ""))
except Exception:
    print("")
PY
)"
  if [[ -z "$BASIC" ]]; then
    echo "    skipped (no credential for ${REG_HOST} in ~/.docker/config.json — cannot verify)" >&2
  else
    CODE="$(curl -sL -o /dev/null -w '%{http_code}' -H "Authorization: Basic ${BASIC}" \
            -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
            "https://${REG_HOST}/v2/${IMG_PATH}/manifests/${TAG}" || echo 000)"
    if [[ "$CODE" != "200" ]]; then
      echo "ERROR: ${REPO}:${TAG} is not in the registry (HTTP ${CODE})." >&2
      echo "       Run build.sh first — deploying now would tear the pod down with nothing to replace it." >&2
      exit 1
    fi
    echo "    ok"
  fi
elif [[ "$REPO" == ghcr.io/* && -n "$TAG" ]] && command -v curl >/dev/null && command -v python3 >/dev/null; then
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
    # Accept BOTH single manifests and manifest lists/indexes. A registry
    # answers 404 — not 406 — when the tag exists but no Accept type matches,
    # so a missing index type reads as "image was never pushed" and blocks a
    # perfectly good deploy. Docker produces an OCI *index* by default now,
    # which means the next rebuild of this image would trip it.
    CODE="$(curl -sL -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" \
            -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
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
