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

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  printf 'Retrieval has tracked local changes; commit or stash them before updating.\n' >&2
  exit 1
fi

git pull --ff-only
"$uv_bin" sync --frozen --python "$venv_python"
"$root/integrate.sh"
printf 'Retrieval updated.\n'
