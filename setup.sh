#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

uv_bin="$(command -v uv || true)"
[[ -n "$uv_bin" ]] || {
  printf 'uv is required: https://docs.astral.sh/uv/\n' >&2
  exit 1
}

if [[ ! -f .env ]]; then
  cp -- .env.example .env
fi
if [[ ! -f sources.toml ]]; then
  cp -- sources.example.toml sources.toml
fi
if [[ ! -f category-overrides.toml ]]; then
  cp -- category-overrides.example.toml category-overrides.toml
fi

if [[ -e .venv && ! -f .venv/pyvenv.cfg ]]; then
  printf '.venv exists but is not a Python virtual environment.\n' >&2
  exit 1
fi

python_spec="${RETRIEVAL_PYTHON:-3.13.12}"
if [[ ! -d .venv ]]; then
  "$uv_bin" venv .venv --python "$python_spec" --seed
fi

venv_python="$root/.venv/bin/python"
[[ -x "$venv_python" ]] || {
  printf 'Virtual-environment Python is missing: %s\n' "$venv_python" >&2
  exit 1
}
"$uv_bin" sync --frozen --python "$venv_python"
chmod +x -- "$root/hooks/session-close.sh"
chmod +x -- "$root/scripts/remove-legacy-iwe.sh"
"$root/scripts/remove-legacy-iwe.sh"
"$venv_python" -c 'from hermes_retrieval.config import Settings; Settings.load().skill_intake_root.mkdir(parents=True, exist_ok=True)'
"$venv_python" -m hermes_retrieval.cli session-close --harness hermes --all-profiles
"$venv_python" -m hermes_retrieval.cli session-close --harness omp --all-profiles
"$venv_python" -m hermes_retrieval.cli integrate

printf 'Retrieval is ready in %s\n' "$root"
printf 'Review .env and sources.toml, then run: %s/start.sh\n' "$root"
