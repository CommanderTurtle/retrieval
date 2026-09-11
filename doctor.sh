#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python="$root/.venv/bin/python"

[[ -x "$python" ]] || {
  printf 'Retrieval virtual-environment Python is missing: %s\n' "$python" >&2
  exit 1
}

exec "$python" -m hermes_retrieval.cli doctor "$@"
