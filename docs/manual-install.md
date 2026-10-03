# Manual install

What `install.sh` does, step by step, for a profile `acme`, project `Acme`, repo `/srv/acme` and owner id
`123456789`. Run `./install.sh --dry-run …` to see the exact commands for your values.

## 1. Tools

```bash
curl -fsSL https://herdr.dev/install.sh | sh
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup --non-interactive
#   (if that site is blocked for you: https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh)
curl -fsSL https://claude.ai/install.sh | bash
npm install -g @fission-ai/openspec
curl -fsSL https://leanctx.com/install.sh | sh
```

## 2. Profile

```bash
hermes profile create acme --no-alias
H=~/.hermes/profiles/acme
TELEGRAM_BOT_TOKEN=... TELEGRAM_ALLOWED_USERS=123456789 TELEGRAM_HOME_CHANNEL=123456789 \
  python3 harness/tools/harness_setup.py env --home $H --force TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS TELEGRAM_HOME_CHANNEL
hermes -p acme config set approvals.mode smart
hermes -p acme config set approvals.timeout 300
hermes -p acme config set approvals.smart_policy "$(cat templates/smart-policy.txt)"
mkdir -p $H/plugins
ln -sfn "$PWD/harness/scripts" $H/scripts
for p in herdr-control herdr-approval-bridge herdr-silence-fix; do
  ln -sfn "$PWD/harness/plugins/$p" $H/plugins/$p
  hermes -p acme plugins enable $p
done
hermes -p acme tools enable herdr --platform telegram
hermes -p acme tools enable file herdr skills terminal --platform webhook
python3 harness/tools/harness_setup.py skills --home $H --src skills \
  --var PROJECT=Acme --var REPO=/srv/acme --var PROFILE=acme
python3 harness/tools/harness_setup.py pipeline --home $H --profile acme --project Acme \
  --repo /srv/acme --owner 123456789 --route herdr-acme
```

## 3. Host gateway webhook listener and route

```bash
hermes config set platforms.webhook.enabled true
hermes config set platforms.webhook.extra.host 127.0.0.1
hermes config set platforms.webhook.extra.port 8650
hermes webhook subscribe herdr-acme --route-profile acme --events test \
  --skills harness-controller --script herdr_workflow_context.py \
  --deliver telegram --deliver-chat-id 123456789 \
  --prompt "$(sed 's/{{PROJECT}}/Acme/g' templates/webhook-prompt.txt)"
```

## 4. Services

```bash
hermes gateway install --start-now --start-on-login     # as root: --system --run-as-user root --start-now
                                                        # already installed: hermes gateway restart
```

Render `templates/systemd/harness-bridge.service.in` (replace every `@TOKEN@`; see `render_unit` in
`install.sh`) to `~/.config/systemd/user/harness-bridge-acme.service` (root:
`/etc/systemd/system/`), then:

```bash
systemctl --user daemon-reload && systemctl --user enable --now harness-bridge-acme.service
sudo loginctl enable-linger $USER    # keep user services running after logout
```

If no Herdr server is managed yet, render `herdr-server.service.in` the same way and enable it.

## 5. Repo and agent

```bash
openspec init --tools claude /srv/acme
cp templates/claude-settings.json /srv/acme/.claude/settings.json   # if it has none
lean-ctx wrap claude
claude                       # log in once
hermes -p acme model         # pick the model Hermes runs on
bin/harness doctor acme
```
