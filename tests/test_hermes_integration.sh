#!/usr/bin/env bash
# Slow integration test (not run in CI): a real Hermes Agent in a clean ubuntu:24.04 container.
# Proves that install.sh wires a real Hermes, that the host gateway serves the profile's route,
# and that the route script runs in the profile's scope: it finds the profile's pipeline config
# and registry and accepts a real bridge event built by the bridge's own code.
# Needs docker and network access. Takes a few minutes (it installs Hermes).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" != "--inner" ]]; then
  exec docker run --rm -v "$ROOT":/w -w /w ubuntu:24.04 bash /w/tests/test_hermes_integration.sh --inner
fi

fail() { echo "FAIL: $*" >&2; [[ -f /tmp/gateway.log ]] && tail -40 /tmp/gateway.log >&2; exit 1; }
step() { printf '\n--- %s\n' "$*"; }

step "system packages"
apt-get update -qq >/dev/null
apt-get install -y -qq curl git python3 ca-certificates xz-utils procps libatomic1 >/dev/null

step "Hermes Agent (official installer, no setup wizard)"
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh |
  bash -s -- --skip-setup --non-interactive >/tmp/hermes-install.log 2>&1 || {
  tail -30 /tmp/hermes-install.log; fail "Hermes install failed"; }
export PATH="$HOME/.local/bin:$PATH"
hermes --version

step "install.sh against the real Hermes"
mkdir -p /srv/acme && git -C /srv/acme init -q
TELEGRAM_BOT_TOKEN="000000000:$(printf 'A%.0s' $(seq 35))" ./install.sh --yes --skip-deps --no-services \
  --no-agent-setup --project Acme --repo /srv/acme --owner-id 111111111 --webhook-port 8650
P="$HOME/.hermes/profiles/acme"
[[ -f "$P/herdr-pipeline.json" ]] || fail "no pipeline config"
hermes webhook list | grep herdr-acme >/dev/null || fail "route not registered on the host gateway"
{ "$ROOT/bin/harness" doctor acme --no-services || true; } | grep "OK    webhook route herdr-acme" >/dev/null ||
  fail "doctor does not see the route"

step "a fake OpenAI-compatible model for the profile (answers [SILENT])"
python3 tests/fake_llm.py 9999 &
hermes -p acme config set model.provider custom >/dev/null
hermes -p acme config set model.default fake >/dev/null
hermes -p acme config set model.base_url http://127.0.0.1:9999/v1 >/dev/null
hermes -p acme config set model.api_key test-key >/dev/null

step "host gateway"
hermes gateway run >/tmp/gateway.log 2>&1 &
for _ in $(seq 90); do
  python3 -c 'import socket; socket.create_connection(("127.0.0.1", 8650), 1)' 2>/dev/null && break
  sleep 1
done
python3 -c 'import socket; socket.create_connection(("127.0.0.1", 8650), 1)' || fail "webhook listener never opened"

step "a payload for no registered task is ignored by the route script (doctor's probe)"
# The listener opens before a first-start gateway has finished preparing: retry like doctor --wait.
for _ in $(seq 40); do
  out="$(hermes webhook test herdr-acme --payload '{"event_id":"doctor","task":{"id":"doctor"}}' 2>&1 || true)"
  grep -q 'Response (200)' <<<"$out" && break
  sleep 3
done
echo "$out" | tail -2
grep -q 'Response (200)' <<<"$out" || fail "route did not answer 200"
grep -q '"reason": "script"' <<<"$out" || fail "route script did not run for the profile route"

step "a real bridge event for a registered task is accepted"
python3 harness/scripts/herdr_registry.py --registry "$P/state/herdr_tasks.json" create \
  '{"id":"acme-demo","pane_id":"w1:p1","cwd":"/srv/acme","change":"demo","phase":"propose"}' >/dev/null
cat >/tmp/deliver.py <<'EOF'
import sys
from pathlib import Path
sys.path.insert(0, "harness/scripts")
import herdr_event_bridge as bridge
registry = Path(sys.argv[1])
tasks, _ = bridge.load_tasks(registry)
state = {"version": 1, "panes": {}, "pending": []}
bridge.transition(state, tasks["acme-demo"], "working")
event = bridge.transition(state, tasks["acme-demo"], "idle")
result = bridge.deliver_payload("hermes", "herdr-acme", event, 60)
print(result)
if result.outcome != "accepted":
    sys.exit(1)
# The bridge keeps one controller per workflow until the run's session is closed in the
# profile's state.db; an unclosed session holds the workflow for 30 minutes.
import time
import webhook_ids
chats = webhook_ids.session_chat_ids("herdr-acme", result.delivery_id, "acme")
chats.append(webhook_ids.session_chat_ids("herdr-acme", result.delivery_id, None)[1])
profile_db = registry.parent.parent / "state.db"
host_db = Path.home() / ".hermes" / "state.db"
for _ in range(90):
    rows = bridge.lookup_controller_sessions(profile_db, chats, 0) or {}
    closed = [rows[c] for c in chats if c in rows and rows[c][1] is not None]
    if closed:
        print(f"controller session closed in the profile state.db: {closed[0]}")
        sys.exit(0)
    time.sleep(2)
print(f"profile db: {bridge.lookup_controller_sessions(profile_db, chats, 0)}")
print(f"host db:    {bridge.lookup_controller_sessions(host_db, chats, 0)}")
sys.exit(2)
EOF
rc=0
python3 /tmp/deliver.py "$P/state/herdr_tasks.json" || rc=$?
[[ $rc == 1 ]] && fail "the bridge event was not accepted"
[[ $rc == 2 ]] && fail "the controller session was never closed in the profile's state.db"
grep -q "silence marker rejected" /tmp/gateway.log &&
  fail "the controller's [SILENT] was treated as a user turn (it would be sent to Telegram)"

echo
echo "hermes integration: OK"
