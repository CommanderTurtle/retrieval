#!/usr/bin/env bash
set -Eeuo pipefail

legacy="$HOME/Hermes/iwe"
if [[ -e "$legacy" ]]; then
  resolved="$(realpath -- "$legacy")"
  [[ "$resolved" == "$HOME/Hermes/iwe" ]] || {
    printf 'Refusing unexpected legacy checkout path: %s\n' "$resolved" >&2
    exit 1
  }
  [[ -d "$resolved/.git" ]] || {
    printf 'Refusing non-Git legacy path: %s\n' "$resolved" >&2
    exit 1
  }
  origin="$(git -C "$resolved" remote get-url origin 2>/dev/null || true)"
  case "$origin" in
    https://github.com/iwe-org/iwe|https://github.com/iwe-org/iwe.git|git@github.com:iwe-org/iwe.git) ;;
    *)
      printf 'Refusing legacy checkout with unexpected origin: %s\n' "$origin" >&2
      exit 1
      ;;
  esac
  [[ -z "$(git -C "$resolved" status --porcelain)" ]] || {
    printf 'Refusing to remove modified legacy checkout: %s\n' "$resolved" >&2
    exit 1
  }
  rm -rf -- "$resolved"
fi

if command -v cargo >/dev/null 2>&1; then
  installed="$(cargo install --list 2>/dev/null || true)"
  grep -q '^iwe v' <<<"$installed" && cargo uninstall iwe
  grep -q '^iwes v' <<<"$installed" && cargo uninstall iwes
else
  for binary in "$HOME/.cargo/bin/iwe" "$HOME/.cargo/bin/iwes"; do
    [[ ! -e "$binary" || "$binary" == "$HOME/.cargo/bin/"* ]] || exit 1
    rm -f -- "$binary"
  done
fi

for registry_root in "$HOME/.cargo/registry/cache" "$HOME/.cargo/registry/src"; do
  [[ -d "$registry_root" ]] || continue
  while IFS= read -r -d '' artifact; do
    resolved="$(realpath -- "$artifact")"
    [[ "$resolved" == "$registry_root/"* ]] || {
      printf 'Refusing unexpected Cargo artifact: %s\n' "$resolved" >&2
      exit 1
    }
    rm -rf -- "$resolved"
  done < <(
    find "$registry_root" -mindepth 2 -maxdepth 2 \
      \( -name 'iwe-[0-9]*' -o -name 'iwe-[0-9]*.crate' \
         -o -name 'iwes-[0-9]*' -o -name 'iwes-[0-9]*.crate' \) \
      -print0
  )
done
