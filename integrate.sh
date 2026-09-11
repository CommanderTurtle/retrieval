#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python="$root/.venv/bin/python"

[[ -x "$python" ]] || {
  printf 'Run %s/setup.sh before integrating Retrieval.\n' "$root" >&2
  exit 1
}

chmod +x -- "$root/hooks/session-close.sh" "$root/scripts/remove-legacy-iwe.sh"
"$root/scripts/remove-legacy-iwe.sh"
"$python" -m hermes_retrieval.cli session-close --harness hermes --all-profiles
"$python" -m hermes_retrieval.cli session-close --harness omp --all-profiles
"$python" -m hermes_retrieval.cli integrate
"$python" -m hermes_retrieval.cli doctor

printf 'Retrieval integrations are reconciled. Restart OMP/Hermes MCP processes to reload code.\n'
