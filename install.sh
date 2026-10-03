#!/usr/bin/env bash
# telegram-harness installer: Herdr + Claude Code + Hermes Agent + lean-ctx + OpenSpec, driven from Telegram.
# Safe to re-run: it converges and never overwrites an edited skill or an existing secret.
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
export PATH="$HOME/.local/bin:$PATH"

PROJECT="" PROFILE="" OWNER="" PORT="" TOKEN_ENV="TELEGRAM_BOT_TOKEN"
REPOS=()
YES=0 SKIP_DEPS=0 NO_SERVICES=0 NO_AGENT_SETUP=0 SIBLING=0
HOME_P=""

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

  --project NAME        project name shown in messages (e.g. Acme)
  --repo PATH           absolute path of the project repo (repeatable)
  --profile NAME        Hermes profile name (default: project name, lowercase alphanumeric)
  --owner-id ID         your numeric Telegram user id (the only account the bot obeys)
  --bot-token-env VAR   read the bot token from this environment variable (default TELEGRAM_BOT_TOKEN)
  --webhook-port N      local port for the Hermes webhook listener (default: first free from 8650)
  --sibling             also route agents working in sibling dirs such as <repo>-worktrees/*
  --yes                 non-interactive: install missing tools, never prompt (missing values are errors)
  --skip-deps           do not install third-party tools
  --no-services         do not install systemd services (containers, CI)
  --no-agent-setup      do not run `lean-ctx wrap claude` or add .claude/settings.json
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
      --owner-id) OWNER="${2:?--owner-id needs a value}"; shift 2 ;;
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
  read -rp "$2 " __reply
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
  if [[ -z "${!TOKEN_ENV:-}" ]] && ! token_saved; then
    [[ "$YES" == 1 ]] && die "set $TOKEN_ENV to the bot token from @BotFather"
    local token; read -rsp "Telegram bot token from @BotFather (hidden): " token; echo
    printf -v "$TOKEN_ENV" '%s' "$token"
  fi
}

token_saved() {
  have hermes || return 1
  local home; home="$(profile_home "$PROFILE")"
  [[ -n "$home" && -f "$home/.env" ]] && grep -q '^TELEGRAM_BOT_TOKEN=.' "$home/.env"
}

validate() {
  [[ "$PROFILE" =~ ^[a-z0-9]+$ ]] || die "--profile must be lowercase letters and digits (got '$PROFILE')"
  [[ "$OWNER" =~ ^-?[0-9]{3,20}$ ]] || die "--owner-id must be your numeric Telegram user id (got '$OWNER')"
  local repo
  for repo in "${REPOS[@]}"; do
    [[ "$repo" == /* ]] || die "--repo must be an absolute path (got '$repo')"
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
    read -rp "Install $bin with: $cmd ? [Y/n] " answer
    [[ "$answer" =~ ^[Nn] ]] && die "$bin is required"
  fi
  run bash -c "$cmd"
  hash -r
  have "$bin" || [[ "$DRY_RUN" == 1 ]] || die "$bin was installed but is not on PATH; add ~/.local/bin to PATH and re-run"
}

install_deps() {
  [[ "$SKIP_DEPS" == 1 ]] && { log "skipping third-party installs (--skip-deps)"; return 0; }
  ensure herdr "curl -fsSL https://herdr.dev/install.sh | sh"
  ensure hermes "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
  ensure claude "curl -fsSL https://claude.ai/install.sh | bash"
  ensure openspec "npm install -g @fission-ai/openspec"
  ensure lean-ctx "curl -fsSL https://leanctx.com/install.sh | sh"
}

helper() { run python3 "$HARNESS_ROOT/harness/tools/harness_setup.py" "$@"; }
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

  if [[ -n "${!TOKEN_ENV:-}" ]]; then
    export TELEGRAM_BOT_TOKEN="${!TOKEN_ENV}"
    helper env --home "$HOME_P" --force TELEGRAM_BOT_TOKEN
  fi
  export TELEGRAM_ALLOWED_USERS="$OWNER" TELEGRAM_HOME_CHANNEL="$OWNER"
  helper env --home "$HOME_P" --force TELEGRAM_ALLOWED_USERS TELEGRAM_HOME_CHANNEL
  if ! grep -qs '^WEBHOOK_SECRET=.' "$HOME_P/.env"; then
    WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    export WEBHOOK_SECRET
    helper env --home "$HOME_P" WEBHOOK_SECRET
  fi

  [[ -n "$PORT" ]] || PORT="$(hermes_config_get "$PROFILE" platforms.webhook.extra.port)"
  [[ -n "$PORT" ]] || PORT="$(free_port)"
  hcfg platforms.webhook.enabled true
  hcfg platforms.webhook.extra.host 127.0.0.1
  hcfg platforms.webhook.extra.port "$PORT"
  hcfg approvals.mode smart
  hcfg approvals.timeout 300
  if [[ -z "$(hermes_config_get "$PROFILE" approvals.smart_policy)" ]]; then
    hcfg approvals.smart_policy "$(cat "$HARNESS_ROOT/templates/smart-policy.txt")"
  fi

  run mkdir -p "$HOME_P/plugins"
  link_dir "$HARNESS_ROOT/harness/scripts" "$HOME_P/scripts"
  local plugin
  for plugin in "${PLUGINS[@]}"; do
    link_dir "$HARNESS_ROOT/harness/plugins/$plugin" "$HOME_P/plugins/$plugin"
    run hermes -p "$PROFILE" plugins enable "$plugin"
  done
  run hermes -p "$PROFILE" tools enable herdr --platform telegram
  run hermes -p "$PROFILE" tools enable file herdr skills terminal --platform webhook

  helper skills --home "$HOME_P" --src "$HARNESS_ROOT/skills" \
    --var "PROJECT=$PROJECT" --var "REPO=${REPOS[0]}" --var "PROFILE=$PROFILE"
  local repo_args=() repo
  for repo in "${REPOS[@]}"; do repo_args+=(--repo "$repo"); done
  local sibling=()
  [[ "$SIBLING" == 1 ]] && sibling=(--sibling)
  helper pipeline --home "$HOME_P" --profile "$PROFILE" --project "$PROJECT" "${repo_args[@]}" \
    "${sibling[@]}" --owner "$OWNER"
}

setup_route() {
  local prompt
  prompt="$(PROJECT="$PROJECT" python3 -c 'import os, sys
print(open(sys.argv[1]).read().replace("{{PROJECT}}", os.environ["PROJECT"]), end="")' \
    "$HARNESS_ROOT/templates/webhook-prompt.txt")"
  run hermes -p "$PROFILE" webhook subscribe "$ROUTE" \
    --description "Herdr lifecycle controller for $PROJECT" --events test \
    --skills harness-controller --script herdr_workflow_context.py \
    --deliver telegram --deliver-chat-id "$OWNER" --prompt "$prompt"
}

render_unit() { # render_unit TEMPLATE OUT
  local user_line="" wanted="default.target" tmp
  if is_root; then user_line="User=root"; wanted="multi-user.target"; fi
  tmp="$(mktemp)"
  sed -e "s|@PROFILE@|$PROFILE|g" -e "s|@GATEWAY_UNIT@|$(gateway_unit "$PROFILE")|g" \
      -e "s|@USER_LINE@|$user_line|g" -e "s|@HOME@|$HOME|g" -e "s|@HERMES_HOME@|$HOME_P|g" \
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
  if is_root; then
    run hermes -p "$PROFILE" gateway install --system --run-as-user root --start-now
  else
    run hermes -p "$PROFILE" gateway install --start-now --start-on-login
  fi
  local units=("$(bridge_unit "$PROFILE")")
  render_unit "$HARNESS_ROOT/templates/systemd/harness-bridge.service.in" "$(unit_dir)/$(bridge_unit "$PROFILE")"
  if [[ ! -f "$(unit_dir)/herdr-server.service" ]]; then
    if pgrep -f "herdr server" >/dev/null 2>&1; then
      warn "a herdr server is already running outside systemd; it will not restart after a reboot"
    else
      render_unit "$HARNESS_ROOT/templates/systemd/herdr-server.service.in" "$(unit_dir)/herdr-server.service"
      units=(herdr-server.service "${units[@]}")
    fi
  fi
  run sysctl daemon-reload
  run sysctl enable --now "${units[@]}"
  if ! is_root && have loginctl && loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q '=no'; then
    warn "user services stop when you log out; run once: sudo loginctl enable-linger $USER"
  fi
}

setup_repos() {
  local repo
  for repo in "${REPOS[@]}"; do
    if [[ ! -d "$repo" ]]; then warn "repo $repo does not exist yet; skipping its OpenSpec setup"; continue; fi
    if [[ -d "$repo/openspec" ]]; then
      log "OpenSpec already initialized in $repo"
    else
      run openspec init --tools claude --no-animation "$repo"
    fi
    if [[ "$NO_AGENT_SETUP" != 1 && ! -f "$repo/.claude/settings.json" ]]; then
      run mkdir -p "$repo/.claude"
      run cp "$HARNESS_ROOT/templates/claude-settings.json" "$repo/.claude/settings.json"
    fi
  done
  if [[ "$NO_AGENT_SETUP" != 1 ]]; then
    run lean-ctx wrap claude </dev/null || warn "lean-ctx wrap claude failed; run it yourself later"
  fi
}

finish() {
  if [[ "$DRY_RUN" == 1 ]]; then log "dry run finished: nothing was changed"; return 0; fi
  local flags=()
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
  setup_route
  setup_services
  setup_repos
  finish
}

main "$@"
