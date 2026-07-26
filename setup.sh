#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

if [[ ! -f .env ]]; then
  cp .env.example .env
fi
if [[ ! -f sources.toml ]]; then
  cp sources.example.toml sources.toml
fi

uv venv --python 3.13.12 --seed
uv sync --frozen

printf 'Hermes Retrieval is ready in %s\n' "$root"
printf 'Review .env and sources.toml, then run: %s\n' "$root/start.sh"

