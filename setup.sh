#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

uv_bin="$(command -v uv || true)"
[[ -n "$uv_bin" ]] || {
  printf 'uv is required: https://docs.astral.sh/uv/\n' >&2
  exit 1
}

projects_dir="${HERMES_PROJECTS_DIR:-$HOME/Hermes}"
iwe_source="${RETRIEVAL_IWE_SOURCE:-$projects_dir/iwe}"
if [[ -e "$iwe_source" && ! -d "$iwe_source/.git" ]]; then
  printf 'IWE source path exists but is not a Git checkout: %s\n' "$iwe_source" >&2
  exit 1
fi
if [[ ! -d "$iwe_source/.git" ]]; then
  git_bin="$(command -v git || true)"
  [[ -n "$git_bin" ]] || {
    printf 'git is required to maintain the IWE source checkout.\n' >&2
    exit 1
  }
  mkdir -p -- "$(dirname -- "$iwe_source")"
  "$git_bin" clone --filter=blob:none https://github.com/iwe-org/iwe.git "$iwe_source"
fi

iwe_bin="$(command -v iwe || true)"
if [[ -z "$iwe_bin" && -x "$HOME/.cargo/bin/iwe" ]]; then
  iwe_bin="$HOME/.cargo/bin/iwe"
fi
if [[ -z "$iwe_bin" ]]; then
  cargo_bin="$(command -v cargo || true)"
  [[ -n "$cargo_bin" ]] || {
    printf 'IWE requires a native Rust toolchain (cargo): https://rustup.rs/\n' >&2
    exit 1
  }
  # Build only the stable CLI integration surface from the visible checkout.
  "$cargo_bin" install --path "$iwe_source/crates/iwe" --locked
  iwe_bin="$HOME/.cargo/bin/iwe"
fi
export RETRIEVAL_IWE_COMMAND="${RETRIEVAL_IWE_COMMAND:-$iwe_bin}"
export RETRIEVAL_IWE_SOURCE="${RETRIEVAL_IWE_SOURCE:-$iwe_source}"

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
"$venv_python" -c 'from hermes_retrieval.config import Settings; Settings.load().skill_intake_root.mkdir(parents=True, exist_ok=True)'
"$venv_python" -m hermes_retrieval.cli catalog sync
"$venv_python" -m hermes_retrieval.cli integrate

printf 'Retrieval is ready in %s\n' "$root"
printf 'Review .env and sources.toml, then run: %s/start.sh\n' "$root"
