#!/usr/bin/env bash
# telegram-harness installer: Herdr + Claude Code + Hermes Agent + lean-ctx + OpenSpec, driven from Telegram.
# Safe to re-run: it converges and never overwrites an edited skill or an existing secret.
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
export PATH="$HOME/.local/bin:$PATH"
DRY_RUN=0  # only --dry-run turns it on, never an inherited variable

PROJECT="" PROFILE="" OWNER="" PORT="" TOKEN_ENV="TELEGRAM_BOT_TOKEN"
REPOS=()
YES=0 SKIP_DEPS=0 NO_SERVICES=0 NO_AGENT_SETUP=0 SIBLING=0 OWNER_GIVEN=0
HOME_P="" HOST_HOME="" BOT_TOKEN=""

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

  --project NAME        project name shown in messages (e.g. Acme)
  --repo PATH           absolute path of the project repo (repeatable)
  --profile NAME        Hermes profile name (default: project name, lowercase alphanumeric)
  --owner-id ID         your numeric Telegram user id (the only account the bot obeys)
  --bot-token-env VAR   read the bot token from this environment variable (default TELEGRAM_BOT_TOKEN)
  --webhook-port N      port of the host gateway's localhost webhook listener, if none is configured yet
                        (default: first free from 8650)
  --sibling             also route agents working in sibling dirs such as <repo>-worktrees/*
  --yes                 non-interactive: install missing tools, never prompt (missing values are errors)
  --skip-deps           do not install third-party tools
  --no-services         do not install systemd services (containers, CI)
  --no-agent-setup      leave Claude Code's own config alone: no `herdr integration install claude`,
                        no `lean-ctx wrap claude`, no .claude/settings.json
  --dry-run             print every action, change nothing
  -h, --help            this help

Re-run with --profile NAME to reuse the answers saved in that profile.
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --project) PROJECT="${2:?--project needs a value}"; shift 2 ;;
      --repo) REPOS+=("${2:?--repo needs a value}"); shift 2 ;;
      --profile) PROFILE="${2:?--profile needs a value}"; shift 2 ;;
      --owner-id) OWNER="${2:?--owner-id needs a value}"; OWNER_GIVEN=1; shift 2 ;;
      --bot-token-env) TOKEN_ENV="${2:?--bot-token-env needs a value}"; shift 2 ;;
      --webhook-port) PORT="${2:?--webhook-port needs a value}"; shift 2 ;;
      --sibling) SIBLING=1; shift ;;
      --yes) YES=1; shift ;;
      --skip-deps) SKIP_DEPS=1; shift ;;
      --no-services) NO_SERVICES=1; shift ;;
      --no-agent-setup) NO_AGENT_SETUP=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
  done
}

ask() { # ask VAR "question"
  local __var="$1" __reply
  [[ "$YES" == 1 ]] && die "missing ${2%%:*} (pass it as a flag with --yes)"
  read -rp "$2 " __reply || die "no input for '${2%%:*}' (no terminal?): pass the values as flags"
  printf -v "$__var" '%s' "$__reply"
}

saved() { # saved FILE KEY: a value from a saved herdr-pipeline.json (lists one per line)
  python3 -c 'import json, sys
value = json.load(open(sys.argv[1])).get(sys.argv[2])
if isinstance(value, list):
    print("\n".join(value))
elif isinstance(value, bool):
    print(str(value).lower())
elif value is not None:
    print(value)' "$1" "$2" 2>/dev/null || true
}

load_saved() {
  [[ -n "$PROFILE" ]] && have hermes || return 0
  local home cfg
  home="$(profile_home "$PROFILE")"
  cfg="$home/herdr-pipeline.json"
  [[ -n "$home" && -f "$cfg" ]] || return 0
  [[ -n "$PROJECT" ]] || PROJECT="$(saved "$cfg" project)"
  [[ -n "$OWNER" ]] || OWNER="$(saved "$cfg" owner_user_id)"
  if [[ ${#REPOS[@]} -eq 0 ]]; then mapfile -t REPOS < <(saved "$cfg" cwd_prefixes); fi
  [[ "$(saved "$cfg" sibling_prefix)" == true ]] && SIBLING=1
  log "reusing saved answers from $cfg"
}

ask_missing() {
  [[ -n "$PROJECT" ]] || ask PROJECT "Project name (e.g. Acme):"
  [[ -n "$PROFILE" ]] || PROFILE="$(tr -cd '[:alnum:]' <<<"$PROJECT" | tr '[:upper:]' '[:lower:]')"
  if [[ ${#REPOS[@]} -eq 0 ]]; then
    local repo; ask repo "Absolute path of the project repo:"; REPOS=("$repo")
  fi
  [[ -n "$OWNER" ]] || ask OWNER "Your numeric Telegram user id (ask @userinfobot):"
  BOT_TOKEN="${!TOKEN_ENV:-}"
  if [[ -z "$BOT_TOKEN" ]] && ! token_saved; then
    [[ "$YES" == 1 ]] && die "set $TOKEN_ENV to the bot token from @BotFather"
    read -rsp "Telegram bot token from @BotFather (hidden): " BOT_TOKEN || die "no input for the bot token"
    echo
  fi
  # Keep the token out of every child process (installers, hermes, openspec, ...).
  export -n "${TOKEN_ENV?}" TELEGRAM_BOT_TOKEN 2>/dev/null || true
}

token_saved() {
  have hermes || return 1
  local home; home="$(profile_home "$PROFILE")"
  [[ -n "$home" && -f "$home/.env" ]] && grep -q '^TELEGRAM_BOT_TOKEN=.' "$home/.env"
}

validate() {
  [[ "$PROFILE" =~ ^[a-z0-9]+$ ]] || die "--profile must be lowercase letters and digits (got '$PROFILE')"
  [[ "$OWNER" =~ ^-?[0-9]{3,20}$ ]] || die "--owner-id must be your numeric Telegram user id (got '$OWNER')"
  local i repo
  for i in "${!REPOS[@]}"; do
    repo="${REPOS[$i]/#\~/$HOME}"
    while [[ "$repo" != / && "$repo" == */ ]]; do repo="${repo%/}"; done
    [[ "$repo" == /* ]] || die "--repo must be an absolute path (got '${REPOS[$i]}')"
    REPOS[i]="$repo"
  done
  [[ -z "$PORT" || "$PORT" =~ ^[0-9]{2,5}$ ]] || die "--webhook-port must be a number"
}

preflight() {
  [[ "$(uname -s)" == Linux ]] || die "telegram-harness supports Linux only"
  have python3 || die "python3 (3.11+) is required"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 3.11+ is required"
  have git || die "git is required"
  have curl || die "curl is required"
  if [[ "$NO_SERVICES" != 1 ]]; then have systemctl || die "systemd is required (or pass --no-services)"; fi
  # Hermes's bundled Node.js links libatomic, which minimal Ubuntu/Debian images lack.
  if [[ "$SKIP_DEPS" != 1 ]] && ! have hermes && have ldconfig && ! ldconfig -p | grep 'libatomic\.so\.1' >/dev/null; then
    die "Hermes Agent needs libatomic1: sudo apt-get install -y libatomic1 (then re-run)"
  fi
  if [[ "$SKIP_DEPS" != 1 ]] && ! have openspec; then
    have npm || die "npm (Node.js 20+) is required to install OpenSpec"
    node -e 'process.exit(parseInt(process.versions.node) < 20 ? 1 : 0)' || die "Node.js 20+ is required"
  fi
}

ensure() { # ensure BIN "install command"
  local bin="$1" cmd="$2" answer
  if have "$bin"; then
    log "$bin: $("$bin" --version 2>/dev/null | head -1 || echo present)"
    return 0
  fi
  if [[ "$YES" != 1 && "$DRY_RUN" != 1 ]]; then
    read -rp "Install $bin with: $cmd ? [Y/n] " answer || die "no input (no terminal?): use --yes"
    [[ "$answer" =~ ^[Nn] ]] && die "$bin is required"
  fi
  run bash -c "set -o pipefail; $cmd"  # a failed download must not look like a successful install
  hash -r
  have "$bin" || [[ "$DRY_RUN" == 1 ]] || die "$bin was installed but is not on PATH; add ~/.local/bin to PATH and re-run"
}

install_deps() {
  [[ "$SKIP_DEPS" == 1 ]] && { log "skipping third-party installs (--skip-deps)"; return 0; }
  ensure herdr "curl -fsSL https://herdr.dev/install.sh | sh"
  # The project site can refuse some networks; the same script is in the repo.
  ensure hermes "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup --non-interactive || curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup --non-interactive"
  ensure claude "curl -fsSL https://claude.ai/install.sh | bash"
  ensure openspec "npm install -g @fission-ai/openspec"
  ensure lean-ctx "curl -fsSL https://leanctx.com/install.sh | sh"
}

helper() { run python3 "$HARNESS_ROOT/harness/tools/harness_setup.py" "$@"; }

store_token() { # the token reaches only this one process, never argv or output
  if [[ "$DRY_RUN" == 1 ]]; then printf '[dry-run] store the bot token in %s/.env\n' "$HOME_P"; return 0; fi
  TELEGRAM_BOT_TOKEN="$BOT_TOKEN" python3 "$HARNESS_ROOT/harness/tools/harness_setup.py" \
    env --home "$HOME_P" --force TELEGRAM_BOT_TOKEN
}
hcfg() { run hermes -p "$PROFILE" config set "$@"; }

link_dir() { # link_dir SRC DST: DST becomes a symlink to SRC (a real dir is moved aside once)
  if [[ -e "$2" && ! -L "$2" ]]; then run mv "$2" "$2.pre-harness"; fi
  run ln -sfn "$1" "$2"
}

free_port() {
  python3 -c 'import socket
for port in range(8650, 8750):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port)); print(port); break
        except OSError:
            pass'
}

setup_profile() {
  if hermes profile show "$PROFILE" >/dev/null 2>&1; then
    log "Hermes profile $PROFILE exists"
  else
    run hermes profile create "$PROFILE" --no-alias
  fi
  HOME_P="$(profile_home "$PROFILE")"
  [[ -n "$HOME_P" ]] || HOME_P="$HOME/.hermes/profiles/$PROFILE"  # dry run before creation

  [[ -z "$BOT_TOKEN" ]] || store_token
  # The allowlist is replaced only when --owner-id is given now; an update run keeps your edits.
  local force=()
  [[ "$OWNER_GIVEN" == 1 ]] && force=(--force)
  export TELEGRAM_ALLOWED_USERS="$OWNER" TELEGRAM_HOME_CHANNEL="$OWNER"
  helper env --home "$HOME_P" "${force[@]}" TELEGRAM_ALLOWED_USERS TELEGRAM_HOME_CHANNEL

  hcfg approvals.mode smart
  hcfg approvals.timeout 300
  if [[ -z "$(profile_config_get "$PROFILE" approvals.smart_policy)" ]]; then
    hcfg approvals.smart_policy "$(cat "$HARNESS_ROOT/templates/smart-policy.txt")"
  fi

  run mkdir -p "$HOME_P/plugins"
  # Launch settings for the coding agents (`claude --settings <file>`): compact at 400k tokens and no
  # prompt suggestions that a stray Enter from the controller could submit. Kept once edited.
  if [[ ! -f "$HOME_P/claude-agent-settings.json" ]]; then
    run cp "$HARNESS_ROOT/templates/claude-agent-settings.json" "$HOME_P/claude-agent-settings.json"
  fi
  link_dir "$HARNESS_ROOT/harness/scripts" "$HOME_P/scripts"
  local plugin
  for plugin in "${PLUGINS[@]}"; do
    link_dir "$HARNESS_ROOT/harness/plugins/$plugin" "$HOME_P/plugins/$plugin"
    run hermes -p "$PROFILE" plugins enable "$plugin"
  done
  run hermes -p "$PROFILE" tools enable herdr --platform telegram
  run hermes -p "$PROFILE" tools enable file herdr skills terminal --platform webhook

  helper skills --home "$HOME_P" --src "$HARNESS_ROOT/skills" \
    --var "PROJECT=$PROJECT" --var "REPO=${REPOS[0]}" --var "PROFILE=$PROFILE" \
    --var "AGENT_SETTINGS=$HOME_P/claude-agent-settings.json"
  local repo_args=() repo
  for repo in "${REPOS[@]}"; do repo_args+=(--repo "$repo"); done
  local sibling=()
  [[ "$SIBLING" == 1 ]] && sibling=(--sibling)
  helper pipeline --home "$HOME_P" --profile "$PROFILE" --project "$PROJECT" "${repo_args[@]}" \
    "${sibling[@]}" --owner "$OWNER" --route "$(route_name "$PROFILE")"
}

# The host gateway owns the one webhook listener (localhost only); every profile's route is
# served on it under /p/<profile>/webhooks/<route>.
setup_host() {
  HOST_HOME="$(host_home)"
  [[ -n "$HOST_HOME" ]] || HOST_HOME="$HOME/.hermes"
  local configured_port
  configured_port="$(host_config_get platforms.webhook.extra.port | grep -Eo '^[0-9]+$' | tail -1 || true)"
  run hhost config set platforms.webhook.enabled true
  if [[ -z "$configured_port" ]]; then
    [[ -n "$PORT" ]] || PORT="$(free_port)"
    run hhost config set platforms.webhook.extra.host 127.0.0.1
    run hhost config set platforms.webhook.extra.port "$PORT"
  elif [[ -n "$PORT" && "$PORT" != "$configured_port" ]]; then
    warn "the host webhook listener already uses port $configured_port; keeping it (ignoring --webhook-port $PORT)"
  fi
}

setup_route() {
  local prompt
  prompt="$(PROJECT="$PROJECT" python3 -c 'import os, sys
print(open(sys.argv[1]).read().replace("{{PROJECT}}", os.environ["PROJECT"]), end="")' \
    "$HARNESS_ROOT/templates/webhook-prompt.txt")"
  # The route's HMAC secret stays in Hermes's subscriptions file, not in the install output.
  run hhost webhook subscribe "$(route_name "$PROFILE")" --route-profile "$PROFILE" \
    --description "Herdr lifecycle controller for $PROJECT" --events test \
    --skills harness-controller --script herdr_workflow_context.py \
    --deliver telegram --deliver-chat-id "$OWNER" --prompt "$prompt" | sed '/[Ss]ecret:/d'
}

render_unit() { # render_unit TEMPLATE OUT
  local user_line="" wanted="default.target" tmp
  if is_root; then user_line="User=root"; wanted="multi-user.target"; fi
  tmp="$(mktemp)"
  sed -e "s|@PROFILE@|$PROFILE|g" -e "s|@GATEWAY_UNIT@|$GATEWAY_UNIT|g" \
      -e "s|@USER_LINE@|$user_line|g" -e "s|@HOME@|$HOME|g" \
      -e "s|@HOST_HOME@|$HOST_HOME|g" -e "s|@PROFILE_HOME@|$HOME_P|g" \
      -e "s|@PATH@|$(dirname "$(command -v hermes)"):$(dirname "$(command -v herdr)"):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin|g" \
      -e "s|@PYTHON@|$(command -v python3)|g" -e "s|@ROOT@|$HARNESS_ROOT|g" -e "s|@SOCKET@|$(herdr_socket)|g" \
      -e "s|@HERMES_BIN@|$(command -v hermes)|g" -e "s|@HERDR_BIN@|$(command -v herdr)|g" \
      -e "s|@WANTED_BY@|$wanted|g" "$1" >"$tmp"
  run mkdir -p "$(dirname "$2")"
  run install -m 0644 "$tmp" "$2"
  rm -f "$tmp"
}

setup_services() {
  [[ "$NO_SERVICES" == 1 ]] && { log "skipping systemd services (--no-services)"; return 0; }
  if ! have hermes || ! have herdr; then
    die "the services need hermes and herdr on PATH (install them or drop --skip-deps)"
  fi
  # One host gateway serves every profile: install it once, otherwise restart it so it picks up
  # this profile, its plugins and its bot token.
  if sysctl cat "$GATEWAY_UNIT" >/dev/null 2>&1; then
    warn "restarting the host gateway: sessions of other profiles running right now are interrupted"
    run hhost gateway restart
  elif is_root; then
    run hhost gateway install --system --run-as-user root --start-now
  else
    run hhost gateway install --start-now --start-on-login
  fi
  local units=("$(bridge_unit "$PROFILE")")
  render_unit "$HARNESS_ROOT/templates/systemd/harness-bridge.service.in" "$(unit_dir)/$(bridge_unit "$PROFILE")"
  if [[ ! -f "$(unit_dir)/herdr-server.service" ]]; then
    if pgrep -u "$(id -u)" -f "herdr server" >/dev/null 2>&1; then
      warn "a herdr server is already running outside systemd; it will not restart after a reboot"
    else
      render_unit "$HARNESS_ROOT/templates/systemd/herdr-server.service.in" "$(unit_dir)/herdr-server.service"
      units=(herdr-server.service "${units[@]}")
    fi
  fi
  run sysctl daemon-reload
  run sysctl enable --now "${units[@]}"
  # A re-run (e.g. after git pull) must put the bridge on the new code and config. Never restart
  # the herdr server here: that would kill every running agent.
  run sysctl try-restart "$(bridge_unit "$PROFILE")"
  local user="${USER:-$(id -un)}"
  if ! is_root && have loginctl && loginctl show-user "$user" -p Linger 2>/dev/null | grep '=no' >/dev/null; then
    warn "user services stop when you log out; run once: sudo loginctl enable-linger $user"
  fi
}

setup_repos() {
  local repo
  for repo in "${REPOS[@]}"; do
    if [[ ! -d "$repo" ]]; then warn "repo $repo does not exist yet; skipping its OpenSpec setup"; continue; fi
    if [[ -d "$repo/openspec" ]]; then
      log "OpenSpec already initialized in $repo"
    elif ! have openspec; then
      warn "openspec is not installed; later run: openspec init --tools claude $repo"
    else
      run openspec init --tools claude --no-animation "$repo"
    fi
    if [[ "$NO_AGENT_SETUP" != 1 && ! -f "$repo/.claude/settings.json" ]]; then
      run mkdir -p "$repo/.claude"
      run cp "$HARNESS_ROOT/templates/claude-settings.json" "$repo/.claude/settings.json"
    fi
  done
  if [[ "$NO_AGENT_SETUP" != 1 ]]; then
    # Claude Code reports its session id to Herdr only through this hook; without it a fresh
    # session before Apply cannot be verified and every task stalls there.
    if ! have herdr; then
      warn "herdr is not installed; later run: herdr integration install claude"
    elif herdr integration status 2>/dev/null | grep '^claude: current' >/dev/null; then
      log "Herdr's Claude Code integration is current"
    else
      run herdr integration install claude
    fi
    if have lean-ctx; then
      run lean-ctx wrap claude </dev/null || warn "lean-ctx wrap claude failed; run it yourself later"
    else
      warn "lean-ctx is not installed; later run: lean-ctx wrap claude"
    fi
  fi
}

finish() {
  if [[ "$DRY_RUN" == 1 ]]; then log "dry run finished: nothing was changed"; return 0; fi
  # Services just started: give the listener and the bridge a minute.
  local flags=(--wait "${HARNESS_DOCTOR_WAIT:-60}")
  [[ "$NO_SERVICES" == 1 ]] && flags=(--no-services)
  "$HARNESS_ROOT/bin/harness" doctor "$PROFILE" "${flags[@]}" || warn "doctor found problems (see FAIL lines above)"
  cat <<EOF

Next steps:
  1. claude                     # log in to Claude Code once
  2. hermes -p $PROFILE model   # pick the LLM provider/model Hermes runs on
  3. Send /start to your bot, then e.g. "Build <feature> in ${REPOS[0]}"
EOF
}

main() {
  parse_args "$@"
  load_saved
  ask_missing
  validate
  preflight
  install_deps
  have hermes || [[ "$DRY_RUN" == 1 ]] || die "hermes is not installed (drop --skip-deps)"
  setup_profile
  setup_host
  setup_route
  setup_services
  setup_repos
  finish
}

main "$@"
