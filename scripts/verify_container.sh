#!/bin/sh
set -eu

image_name="${1:-gatescope-api:phase0}"
uid="$(docker run --rm --entrypoint id "$image_name" -u)"
if [ "$uid" = "0" ]; then
  echo "container unexpectedly runs as root" >&2
  exit 1
fi

container_id="$(docker create "$image_name")"
trap 'docker rm -f "$container_id" >/dev/null 2>&1 || true' EXIT
listing="$(docker export "$container_id" | tar -tf -)"
if printf '%s\n' "$listing" | grep -E '(^|/)(\.git|\.env|data/[^.]|gatescope_executor|tests)(/|$)' >/dev/null; then
  echo "forbidden material found in API image" >&2
  exit 1
fi
echo "container boundary passed (uid=$uid)"

