# Troubleshooting

Start with `bin/harness doctor <profile>`; every FAIL line carries a hint. `bin/harness status <profile>`
shows the services, the registered tasks and the last bridge log lines. Right after a restart, give the
services a minute: `bin/harness doctor <profile> --wait 60`.

| Symptom | Likely cause | Fix |
|---|---|---|
| doctor: `webhook answers` FAIL | host gateway not running, the route missing, or the profile not served by the host gateway | `systemctl status hermes-gateway` (`--user` when not root); re-run `./install.sh --profile <profile>`; see "host gateway" below |
| doctor: `herdr integration for Claude Code` FAIL | Claude Code does not report its session id to Herdr, so fresh sessions before Apply cannot be verified | `herdr integration install claude` |
| doctor: `bridge subscribed to Herdr` FAIL | Herdr server not running or socket path differs | `herdr server`; check `~/.config/herdr/herdr.sock`; `bin/harness status <profile>` |
| The bot does not answer at all | wrong token, your id is not in `TELEGRAM_ALLOWED_USERS`, or the token is used by another profile | re-run `./install.sh --profile <profile> --owner-id <id>` with the right token; one bot per profile |
| Bridge log shows `dropped … reason=script` | the task's project or cwd does not match `herdr-pipeline.json` | create tasks through the bot; check `cwd_prefixes` (and `--sibling` for worktrees) |
| An agent sits idle and nobody reacts | a wait without a real end, or a question that never reached you | `bin/harness status <profile>`; ask the bot "status?" — it fixes stalls it finds and notes the cause |
| The bot asks you to `/login` | an agent's Claude Code session expired | open the pane (`herdr`), run `/login`, then tell the bot "done" |
| Approval prompt timed out at night | a controller command was not on the read-only list | add the command pattern to the smart policy (`hermes -p <profile> config set approvals.smart_policy …`) |
| After `hermes update` plugins misbehave | Hermes internals changed | `bin/harness test-plugins`; update this repo (`git pull && ./install.sh --profile <profile>`) |
| After `hermes update` the bridge log shows `Connection refused` | the update moved per-profile gateways onto the host gateway (older setups) and their old webhook ports are gone | `./install.sh --profile <profile>`: it registers the route on the host gateway and rewrites the bridge unit |

## Host gateway

Hermes serves every profile from one host gateway (the default profile's `hermes-gateway.service`). It
does not serve a profile that still runs a gateway of its own (from an older Hermes setup) or whose bot
token another profile already uses. Then the route answers 404 or the profile's Telegram adapter stays
parked. Move such profiles onto the host gateway with `hermes gateway migrate` (see `hermes gateway
--help`) and give every profile its own bot token. `hermes update --yes` may run that migration by
itself, even when `hermes update --plan` only listed restarts; re-run `./install.sh --profile <profile>`
afterwards.

## Logs

```bash
journalctl -u harness-bridge-<profile> -f      # --user-unit when not root
journalctl -u hermes-gateway -f
hermes -p <profile> logs
```

Plugin tests that patch Hermes internals run inside Hermes's own runtime (pytest is installed into
`~/.cache/telegram-harness/pytest`, never into Hermes):

```bash
bin/harness test-plugins
```
