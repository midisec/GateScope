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
# Dependency wheels and the base OS may ship their own tests. Check project
# tests in the application tree, and reject repository secrets and execution
# code everywhere in the image.
if printf '%s\n' "$listing" | awk '
  /(^|\/)(\.git|\.env|gatescope_executor)(\/|$)/ { found=1 }
  /^app\/data\/.+/ { found=1 }
  /^app\// && !/^app\/\.venv\// && /\/tests(\/|$)/ { found=1 }
  END { exit !found }
'; then
  echo "forbidden material found in API image" >&2
  exit 1
fi
echo "container boundary passed (uid=$uid)"

