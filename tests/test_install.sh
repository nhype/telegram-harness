#!/usr/bin/env bash
# Installer tests against stub CLIs. `--inner` runs them here; without it they run inside a
# clean ubuntu:24.04 container (needs docker).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" != "--inner" ]]; then
  exec docker run --rm -v "$ROOT":/w -w /w ubuntu:24.04 bash -c \
    'apt-get update -qq >/dev/null && apt-get install -y -qq python3 git curl >/dev/null && tests/test_install.sh --inner'
fi

fail() { echo "FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK/home" FAKE_HOME="$WORK/home/.hermes" STUB_LOG="$WORK/stub.log"
export PATH="$ROOT/tests/stubs/bin:$PATH" TELEGRAM_BOT_TOKEN="123:secret-token-value"
unset HERMES_HOME
mkdir -p "$FAKE_HOME" "$WORK/repo"
: > "$STUB_LOG"
ARGS=(--yes --skip-deps --no-services --no-agent-setup --project Acme --repo "$WORK/repo" --owner-id 111111111 --webhook-port 8699)

# 1. Dry run: plans the work, writes nothing, never shows the token.
out="$("$ROOT/install.sh" --dry-run "${ARGS[@]}" 2>&1)" || fail "dry run exited non-zero: $out"
for want in "profile create acme" "webhook subscribe herdr-acme" "--route-profile acme" \
            "plugins enable herdr-control" "harness_setup.py pipeline"; do
  grep -q -- "$want" <<<"$out" || fail "dry run does not plan: $want"
done
grep -q "secret-token-value" <<<"$out" && fail "dry run printed the bot token"
[[ -d "$FAKE_HOME/profiles/acme" ]] && fail "dry run created the profile"

# 2. Real run against the stubs.
out="$("$ROOT/install.sh" "${ARGS[@]}" 2>&1)" || fail "install exited non-zero: $out"
grep -q "secret-token-value" <<<"$out" && fail "install printed the bot token"
P="$FAKE_HOME/profiles/acme"
[[ "$(stat -c %a "$P/herdr-pipeline.json")" == 600 ]] || fail "pipeline config is not 0600"
grep -q '"route": "herdr-acme"' "$P/herdr-pipeline.json" || fail "pipeline route is not herdr-acme"
[[ "$(stat -c %a "$P/.env")" == 600 ]] || fail ".env is not 0600"
grep -q "^TELEGRAM_BOT_TOKEN=123:secret-token-value$" "$P/.env" || fail ".env lacks the bot token"
grep -q "^TELEGRAM_ALLOWED_USERS=111111111$" "$P/.env" || fail ".env lacks the owner allowlist"
grep -q "^WEBHOOK_SECRET=." "$FAKE_HOME/.env" || fail "host .env lacks a webhook secret"
[[ "$(readlink "$P/scripts")" == "$ROOT/harness/scripts" ]] || fail "scripts is not linked"
for plugin in herdr-control herdr-approval-bridge herdr-silence-fix; do
  [[ -f "$P/plugins/$plugin/__init__.py" ]] || fail "plugin $plugin is not linked"
done
grep -q "{{" "$P/skills/harness-controller/SKILL.md" && fail "controller skill has unrendered placeholders"
grep -q "Acme" "$P/skills/harness-controller/SKILL.md" || fail "controller skill not rendered for Acme"
grep -q "openspec init --tools claude" "$STUB_LOG" || fail "repo was not initialized with openspec"
grep -q "^hermes config set platforms.webhook.extra.port 8699$" "$STUB_LOG" || fail "host webhook port not configured"
grep -q "^hermes -p acme config set platforms.webhook" "$STUB_LOG" && fail "webhook listener configured on the profile, not the host"
grep -q "tools enable file herdr skills terminal --platform webhook" "$STUB_LOG" || fail "webhook toolsets not enabled"
grep -q "lean-ctx wrap" "$STUB_LOG" && fail "--no-agent-setup still wrapped claude"
doctor="$("$ROOT/bin/harness" doctor acme --no-services 2>&1 || true)"
grep -q "OK    webhook route herdr-acme" <<<"$doctor" || fail "doctor does not see the route: $(grep route <<<"$doctor")"
grep -q "OK    herdr-pipeline.json" <<<"$doctor" || fail "doctor rejects the pipeline config"

# 3. Rerun converges and keeps user edits.
echo "my project notes" >> "$P/skills/harness-controller/SKILL.md"
creates_before="$(grep -c "profile create" "$STUB_LOG")"
"$ROOT/install.sh" "${ARGS[@]}" >/dev/null 2>&1 || fail "rerun exited non-zero"
[[ "$(grep -c "profile create" "$STUB_LOG")" == "$creates_before" ]] || fail "rerun created the profile again"
grep -q "my project notes" "$P/skills/harness-controller/SKILL.md" || fail "rerun overwrote an edited skill"
[[ "$(grep -c "^TELEGRAM_BOT_TOKEN=" "$P/.env")" == 1 ]] || fail "rerun duplicated .env keys"

# 4. Bad input is refused before anything is written.
rm -rf "$FAKE_HOME" && mkdir -p "$FAKE_HOME"
"$ROOT/install.sh" --yes --skip-deps --no-services --no-agent-setup --project Acme --repo relative/path \
  --owner-id 111111111 >/dev/null 2>&1 && fail "relative --repo was accepted"
"$ROOT/install.sh" --yes --skip-deps --no-services --no-agent-setup --project Acme --repo "$WORK/repo" \
  --owner-id me >/dev/null 2>&1 && fail "non-numeric --owner-id was accepted"
[[ -d "$FAKE_HOME/profiles/acme" ]] && fail "refused input still created a profile"

# 5. Services: the host gateway (installed per uid, or restarted) and a rendered bridge unit.
SVC=(--yes --skip-deps --no-agent-setup --project Acme --repo "$WORK/repo" --owner-id 111111111 --webhook-port 8699)
SVC_PATH="$ROOT/tests/stubs/services:$PATH"
out="$(FAKE_UID=1000 PATH="$SVC_PATH" "$ROOT/install.sh" --dry-run "${SVC[@]}" 2>&1)" || fail "non-root dry run: $out"
grep -q "hermes gateway install --start-now --start-on-login" <<<"$out" || fail "non-root: host gateway not installed as a user service"
grep -q "$HOME/.config/systemd/user/harness-bridge-acme.service" <<<"$out" || fail "non-root: bridge unit not in the user unit dir"
out="$(FAKE_UID=0 PATH="$SVC_PATH" "$ROOT/install.sh" --dry-run "${SVC[@]}" 2>&1)" || fail "root dry run: $out"
grep -q "hermes gateway install --system --run-as-user root --start-now" <<<"$out" || fail "root: host gateway not installed as a system service"
grep -q "/etc/systemd/system/harness-bridge-acme.service" <<<"$out" || fail "root: bridge unit not in /etc/systemd/system"
out="$(FAKE_UID=0 FAKE_GATEWAY_INSTALLED=1 PATH="$SVC_PATH" "$ROOT/install.sh" --dry-run "${SVC[@]}" 2>&1)" || fail "restart dry run: $out"
grep -q "hermes gateway restart" <<<"$out" || fail "an installed host gateway is not restarted"
grep -q "gateway install" <<<"$out" && fail "an installed host gateway is installed again"

UNITS="$WORK/units"
FAKE_UID=1000 HARNESS_UNIT_DIR="$UNITS" PATH="$SVC_PATH" "$ROOT/install.sh" "${SVC[@]}" >/dev/null 2>&1 ||
  fail "service install against stubs failed"
UNIT="$UNITS/harness-bridge-acme.service"
[[ -f "$UNIT" ]] || fail "bridge unit was not written"
grep -q "@[A-Z_]*@" "$UNIT" && fail "bridge unit has unrendered tokens"
grep -q "^Environment=HERMES_HOME=$FAKE_HOME$" "$UNIT" || fail "bridge does not talk to the host gateway home"
grep -q -- "--state-db $FAKE_HOME/profiles/acme/state.db" "$UNIT" || fail "bridge does not read the profile's state.db"
grep -q -- "--config $FAKE_HOME/profiles/acme/herdr-pipeline.json" "$UNIT" || fail "bridge does not read the profile's pipeline"

echo "install tests: OK"
