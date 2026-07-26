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
printf 'Hermes Retrieval updated. Restart the Hermes gateway to reload its MCP process.\n'
