# Security

- **Only you can talk to the bot.** The profile's `TELEGRAM_ALLOWED_USERS` holds your user id; the
  approval bridge accepts `/approve` / `/deny` only from `owner_user_id`, and only as a reply bound to the
  exact request.
- **Agents are powerful.** Claude Code runs with `--dangerously-skip-permissions` so it can work
  unattended. Give it a dedicated server, VM or Unix user, and keep production credentials it should not
  use out of its reach.
- **Smart approvals.** Commands of Hermes's own controller runs go through `approvals.mode: smart`:
  read-only checks are approved automatically, destructive ones are refused or escalated to you (see
  `templates/smart-policy.txt`; edit it to fit your project).
- **Secrets stay local.** The bot token and webhook secret live in the profiles' `.env` files (mode
  0600); `herdr-pipeline.json` is 0600 too. The installer passes the token through the environment only —
  it never appears in a command line, a log or `--dry-run` output.
- **The webhook listens on 127.0.0.1.** Only local processes (the bridge) can post events, and routes
  are HMAC-signed.
- **No pane text leaves the host in payloads.** The bridge sends status metadata, not terminal output;
  the controller reads panes through the local Herdr API.
- **What the controller asks you about.** Money beyond the stated budget, destructive or irreversible
  production actions (deleting data, rotating live secrets), user-visible product choices and your own
  accounts. Everything else it decides and reports.
