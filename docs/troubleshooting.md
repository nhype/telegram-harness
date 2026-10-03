# Troubleshooting

Start with `bin/harness doctor <profile>`; every FAIL line carries a hint. `bin/harness status <profile>`
shows the services, the registered tasks and the last bridge log lines.

| Symptom | Likely cause | Fix |
|---|---|---|
| doctor: `webhook answers` FAIL | host gateway not running, or no model configured | `systemctl status hermes-gateway` (`--user` when not root); `hermes -p <profile> model` |
| doctor: `bridge subscribed to Herdr` FAIL | Herdr server not running or socket path differs | `herdr server`; check `~/.config/herdr/herdr.sock`; `bin/harness status <profile>` |
| The bot does not answer at all | wrong token, or your id is not in `TELEGRAM_ALLOWED_USERS` | re-run `./install.sh --profile <profile>` with the right `--owner-id` / token |
| Bridge log shows `dropped … reason=script` | the task's project or cwd does not match `herdr-pipeline.json` | create tasks through the bot; check `cwd_prefixes` (and `--sibling` for worktrees) |
| An agent sits idle and nobody reacts | a wait without a real end, or a question that never reached you | `bin/harness status <profile>`; ask the bot "status?" — it fixes stalls it finds and notes the cause |
| The bot asks you to `/login` | an agent's Claude Code session expired | open the pane (`herdr`), run `/login`, then tell the bot "done" |
| Approval prompt timed out at night | a controller command was not on the read-only list | add the command pattern to the smart policy (`hermes -p <profile> config set approvals.smart_policy …`) |
| After `hermes update` plugins misbehave | Hermes internals changed | `bin/harness doctor`; update this repo (`git pull && ./install.sh --profile <profile>`) |

Logs:

```bash
journalctl -u harness-bridge-<profile> -f      # --user-unit when not root
journalctl -u hermes-gateway -f
hermes -p <profile> logs
```

Plugin tests that patch Hermes internals run only with Hermes's own interpreter:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest harness/plugins
```
