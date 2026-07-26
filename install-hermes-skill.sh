#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
profile=""

if [[ ${1:-} == "--profile" ]]; then
  profile="${2:?profile name is required}"
elif [[ $# -ne 0 ]]; then
  printf 'usage: %s [--profile NAME]\n' "$0" >&2
  exit 2
fi

target_root="$hermes_home"
if [[ -n "$profile" ]]; then
  target_root="$hermes_home/profiles/$profile"
  [[ -d "$target_root" ]] || {
    printf 'Hermes profile does not exist: %s\n' "$profile" >&2
    exit 1
  }
fi

target="$target_root/skills/research/retrieve-knowledge"
install -d "$target/agents"
install -m 0644 "$root/skills/retrieve-knowledge/SKILL.md" "$target/SKILL.md"
install -m 0644 \
  "$root/skills/retrieve-knowledge/agents/openai.yaml" \
  "$target/agents/openai.yaml"

if [[ -n "$profile" ]]; then
  printf 'Installed retrieve-knowledge for Hermes profile %s.\n' "$profile"
else
  printf 'Installed retrieve-knowledge for the default Hermes profile.\n'
fi
