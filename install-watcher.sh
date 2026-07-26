#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command_path="$root/.venv/bin/hermes-retrieval"
unit_name="hermes-retrieval-watcher.service"
unit_root="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_path="$unit_root/$unit_name"
launcher_root="$HOME/.local/bin"
launcher_path="$launcher_root/hermes-retrieval-watcher"
action="${1:-install}"

[[ -x "$command_path" ]] || {
  printf 'Run %s/setup.sh before installing the watcher.\n' "$root" >&2
  exit 1
}
command -v systemctl >/dev/null 2>&1 || {
  printf 'systemctl is required for the persistent Retrieval watcher.\n' >&2
  exit 1
}

render_launcher() {
  printf '#!/usr/bin/env bash\n'
  printf 'set -Eeuo pipefail\n'
  printf 'exec %q watch\n' "$command_path"
}

render_unit() {
  cat <<'EOF'
[Unit]
Description=Hermes Retrieval source watcher
Documentation=https://github.com/CommanderTurtle/retrieval

[Service]
Type=simple
ExecStart=%h/.local/bin/hermes-retrieval-watcher
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
Environment=NO_TELEMETRY=1
Environment=DO_NOT_TRACK=1

[Install]
WantedBy=default.target
EOF
}

watcher_current() {
  [[ -f "$launcher_path" && -f "$unit_path" ]] || return 1
  cmp --silent "$launcher_path" <(render_launcher) || return 1
  cmp --silent "$unit_path" <(render_unit) || return 1
  systemctl --user is-enabled --quiet "$unit_name" || return 1
  systemctl --user is-active --quiet "$unit_name" || return 1
}

case "$action" in
  install)
    mkdir -p -- "$launcher_root" "$unit_root"
    launcher_temp="$(mktemp --tmpdir="$launcher_root" .hermes-retrieval-watcher.XXXXXX)"
    unit_temp="$(mktemp --tmpdir="$unit_root" .hermes-retrieval-watcher.XXXXXX)"
    trap 'rm -f -- "${launcher_temp:-}" "${unit_temp:-}"' EXIT
    render_launcher >"$launcher_temp"
    render_unit >"$unit_temp"
    chmod 0755 "$launcher_temp"
    chmod 0644 "$unit_temp"
    mv -f -- "$launcher_temp" "$launcher_path"
    mv -f -- "$unit_temp" "$unit_path"
    systemctl --user daemon-reload
    systemctl --user enable --now "$unit_name"
    systemctl --user restart "$unit_name"
    watcher_current || {
      systemctl --user status --no-pager "$unit_name" >&2 || true
      exit 1
    }
    printf 'Hermes Retrieval watcher is active and source-driven.\n'
    ;;
  status)
    watcher_current
    printf 'Hermes Retrieval watcher is current and active.\n'
    ;;
  *)
    printf 'Usage: %s [install|status]\n' "$0" >&2
    exit 2
    ;;
esac
