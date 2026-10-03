# shellcheck shell=bash
# Shared helpers for install.sh and bin/harness.
#
# Hermes runs ONE host gateway (the default profile) that serves every named profile. The
# harness profile keeps its own config, skills, plugins, bot token and state; its webhook route
# lives on the host gateway, bound to the profile with --route-profile.

HARNESS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HARNESS_ROOT
DRY_RUN="${DRY_RUN:-0}"
# shellcheck disable=SC2034  # used by the scripts that source this file
PLUGINS=(herdr-control herdr-approval-bridge herdr-silence-fix)
# shellcheck disable=SC2034
GATEWAY_UNIT="hermes-gateway.service"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
is_root() { [[ "$(id -u)" == 0 ]]; }

# run CMD...: execute, or only print it under --dry-run.
run() {
  if [[ "$DRY_RUN" == 1 ]]; then
    local shown=("$@")
    case "${shown[0]}" in  # show the real commands behind the helpers
      hhost) shown[0]=hermes ;;
      sysctl) if is_root; then shown[0]=systemctl; else shown[0]="systemctl --user"; fi ;;
    esac
    printf '[dry-run] %s\n' "${shown[*]}"
  else
    "$@"
  fi
}

unit_dir() {
  if [[ -n "${HARNESS_UNIT_DIR:-}" ]]; then echo "$HARNESS_UNIT_DIR"
  elif is_root; then echo /etc/systemd/system
  else echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; fi
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
route_name() { echo "herdr-$1"; }
bridge_unit() { echo "harness-bridge-$1.service"; }

# hhost ARGS...: hermes on the host (default) home, whatever HERMES_HOME the shell exported.
hhost() { env -u HERMES_HOME hermes "$@"; }

_home_from_config_path() {
  local path
  path="$(tail -1 || true)"
  [[ "$path" == /*config.yaml ]] && dirname "$path"
  return 0
}

# profile_home P: the profile's HERMES_HOME, or nothing when the profile does not exist.
profile_home() { { hermes -p "$1" config path 2>/dev/null || true; } | _home_from_config_path; }
host_home() { { hhost config path 2>/dev/null || true; } | _home_from_config_path; }

_config_value() {
  local value
  value="$(tail -1 || true)"
  case "$value" in "" | "Config key not set"* | None | null) return 0 ;; esac
  printf '%s\n' "$value"
}

# profile_config_get P KEY / host_config_get KEY: the value, or nothing when the key is not set.
profile_config_get() { { hermes -p "$1" config get "$2" 2>/dev/null || true; } | _config_value; }
host_config_get() { { hhost config get "$1" 2>/dev/null || true; } | _config_value; }
