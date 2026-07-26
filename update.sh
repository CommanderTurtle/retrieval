#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

git pull --ff-only
uv sync --frozen
printf 'Hermes Retrieval updated. Restart the Hermes gateway to reload its MCP process.\n'

