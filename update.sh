#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

uv_bin="$(command -v uv || true)"
[[ -n "$uv_bin" ]] || {
  printf 'uv is required: https://docs.astral.sh/uv/\n' >&2
  exit 1
}
venv_python="$root/.venv/bin/python"
[[ -x "$venv_python" ]] || {
  printf 'Run %s/setup.sh before updating.\n' "$root" >&2
  exit 1
}

git pull --ff-only
"$uv_bin" sync --frozen --python "$venv_python"
chmod +x -- "$root/hooks/session-close.sh"
chmod +x -- "$root/scripts/remove-legacy-iwe.sh"
"$root/scripts/remove-legacy-iwe.sh"
"$venv_python" -m hermes_retrieval.cli session-close --harness hermes --all-profiles
"$venv_python" -m hermes_retrieval.cli session-close --harness omp --all-profiles
"$venv_python" -m hermes_retrieval.cli integrate
printf 'Retrieval updated. Restart OMP/Hermes MCP processes to reload code.\n'
