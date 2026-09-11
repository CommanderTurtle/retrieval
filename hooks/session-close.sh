#!/usr/bin/env bash
set -u

harness="${1:-}"
case "$harness" in
  hermes)
    harness_home="${HERMES_HOME:-$HOME/.hermes}"
    ;;
  omp)
    harness_home="${PI_CODING_AGENT_DIR:-$HOME/.omp/agent}"
    ;;
  *)
    exit 0
    ;;
esac

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$root/.venv/bin/python"
[[ -x "$python_bin" ]] || exit 0

log_root="${XDG_STATE_HOME:-$HOME/.local/state}/retrieval"
mkdir -p -- "$log_root"

# Harness shutdown budgets are deliberately short.  The worker owns its own
# lock, so simultaneous profile exits coalesce safely without holding up the UI.
nohup "$python_bin" -m hermes_retrieval.cli session-close \
  --harness "$harness" --home "$harness_home" \
  >>"$log_root/session-close.log" 2>&1 </dev/null &
exit 0
