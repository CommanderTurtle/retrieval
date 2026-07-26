#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

venv_python="$root/.venv/bin/python"
[[ -x "$venv_python" ]] || {
  printf 'Run %s/setup.sh before starting Retrieval.\n' "$root" >&2
  exit 1
}

exec "$venv_python" -m hermes_retrieval.server
