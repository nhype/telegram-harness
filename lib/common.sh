# shellcheck shell=bash
# Shared helpers for install.sh and bin/harness.

HARNESS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HARNESS_ROOT
DRY_RUN="${DRY_RUN:-0}"
# shellcheck disable=SC2034  # used by the scripts that source this file
PLUGINS=(herdr-control herdr-approval-bridge herdr-silence-fix)
# shellcheck disable=SC2034
ROUTE="herdr-agent-events"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
is_root() { [[ "$(id -u)" == 0 ]]; }

# run CMD...: execute, or only print it under --dry-run.
run() {
  if [[ "$DRY_RUN" == 1 ]]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

unit_dir() {
  if is_root; then echo /etc/systemd/system; else echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; fi
}

# sysctl ARGS...: systemctl for the system (root) or the user manager.
sysctl() {
  if is_root; then systemctl "$@"; else systemctl --user "$@"; fi
}

journal() {
  local unit="$1"; shift
  if is_root; then journalctl -u "$unit" "$@"; else journalctl --user-unit "$unit" "$@"; fi
}

herdr_socket() { echo "${XDG_CONFIG_HOME:-$HOME/.config}/herdr/herdr.sock"; }

# profile_home P: the profile's HERMES_HOME, or nothing when the profile does not exist.
profile_home() {
  local path
  path="$(hermes -p "$1" config path 2>/dev/null | tail -1 || true)"
  [[ "$path" == /*config.yaml ]] && dirname "$path"
  return 0
}

# hermes_config_get P KEY: the value, or nothing when the key is not set.
hermes_config_get() {
  local value
  value="$(hermes -p "$1" config get "$2" 2>/dev/null | tail -1 || true)"
  case "$value" in "" | "Config key not set"* | None | null) return 0 ;; esac
  printf '%s\n' "$value"
}

bridge_unit() { echo "harness-bridge-$1.service"; }
gateway_unit() { echo "hermes-gateway-$1.service"; }
