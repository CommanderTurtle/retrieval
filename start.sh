#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

venv_python="$root/.venv/bin/python"
[[ -x "$venv_python" ]] || {
  printf 'Run %s/setup.sh before starting Retrieval.\n' "$root" >&2
  exit 1
}

export RETRIEVAL_HARNESS="${RETRIEVAL_HARNESS:-hermes}"
case "$RETRIEVAL_HARNESS" in
  hermes|omp) ;;
  *)
    printf 'RETRIEVAL_HARNESS must be hermes or omp.\n' >&2
    exit 2
    ;;
esac

exec "$venv_python" -m hermes_retrieval.server
