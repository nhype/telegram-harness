# telegram-harness

[English](README.md) · [Русский](README.ru.md)

**Run Claude Code agents from Telegram.** You write a task to your bot; Hermes Agent starts a Claude Code
agent in a Herdr terminal pane, drives it through the OpenSpec lifecycle (plan → code → test → deploy →
archive) and messages you only when it needs a decision or has a result.

```
 you ── Telegram ──▶ Hermes Agent (host gateway) ──▶ your profile
                        │  chat: takes tasks, answers "status?"         skill: harness-delivery
                        │  webhook route ◀── events ── bridge ◀── Herdr server (pane status)
                        │  controller run per event                     skill: harness-controller
                        ▼
                     Herdr panes: Claude Code agents working in your repo (OpenSpec + lean-ctx)
```

- **Herdr** runs the agents in terminal panes and reports every status change (working / idle / done).
- **The bridge** (`harness/scripts/herdr_event_bridge.py`) debounces those events and wakes one
  controller run per task at a time; a watchdog wakes it again when a task stalls or a wait ends.
- **Hermes Agent** is the manager: the chat side starts tasks; the controller side reads the pane,
  decides the next step and prompts the agent. It decides technical questions itself and asks you only
  about money, irreversible actions, product choices and your own accounts.
- **OpenSpec** gives every task a plan, a checklist and an archive; **lean-ctx** keeps the agents'
  context small.

## What you need

- A Linux server with systemd (a dedicated VM or user is best: the agents run with
  `--dangerously-skip-permissions`).
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and your numeric user id
  (ask [@userinfobot](https://t.me/userinfobot)).
- A Claude subscription or API key for Claude Code.
- An LLM provider for Hermes (OpenRouter, Anthropic, OpenAI, Nous Portal, …).
- python3 ≥ 3.11, git, curl; Node.js ≥ 20 + npm (for OpenSpec); `libatomic1` (minimal Ubuntu images
  lack it: `sudo apt-get install -y libatomic1`).

## Quick start

```bash
git clone https://github.com/nhype/telegram-harness
cd telegram-harness
./install.sh
```

The installer asks for the project name, the repo path, your Telegram id and the bot token (hidden),
installs whatever is missing (Herdr, Hermes Agent, Claude Code, OpenSpec, lean-ctx — from their official
installers), sets up a Hermes profile for the project and starts the services. Then:

```bash
claude                      # log in to Claude Code once
hermes -p <profile> model   # pick the model Hermes runs on
bin/harness doctor <profile>
```

and send `/start` to your bot. Keep the clone where it is: the profile links its scripts and plugins
into it. Non-interactive install (the token is read without landing in your shell history):

```bash
read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN
./install.sh --yes --project Acme --repo /srv/acme --owner-id 123456789
```

Use a bot token of its own for each project: two Hermes profiles cannot share one bot.

Run `./install.sh --help` for every option (`--dry-run` prints the plan without changing anything).

## Daily use

Write to the bot like to a colleague:

- *"Add CSV export to the reports page"* → the bot starts an agent, which writes an OpenSpec proposal,
  then implements, tests, deploys and archives it; you get a short message when it is live.
- *"status?"* → what is done, what is left, whether agents work or wait, what is needed from you.
- When the bot asks something (rarely), answer in plain words: *"yes"*, *"2"*, *"go ahead"* — the reply
  goes to the agent that asked.

Tell the controller how your project deploys and how to smoke-test it: edit the **Project notes** at the
end of `~/.hermes/profiles/<profile>/skills/harness-controller/SKILL.md`. The installer never overwrites
that file once you edit it.

## Updating

```bash
git pull
./install.sh --profile <profile>   # reuses your saved answers; safe to re-run
```

## Uninstall

```bash
bin/harness uninstall-services <profile>   # bridge + webhook route; the shared Hermes gateway stays
hermes profile delete <profile>            # optional: the profile and its history
```

## Components

| Component | Role | Tested version | License |
|---|---|---|---|
| [Herdr](https://herdr.dev) | terminal workspace for agents, status events | 0.8.0 | Apache-2.0 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Telegram gateway, chat + controller runs | 0.21.4 | MIT |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | the coding agent | 2.1.288 | commercial |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | spec-driven change workflow | 1.13.1 | MIT |
| [lean-ctx](https://github.com/yvgude/lean-ctx) | context compression for agents | 3.10.2 | Apache-2.0 |

The harness itself is the bridge, the task registry, the route script, three Hermes plugins, two skills
and the installer. The plugins patch a few Hermes gateway internals, so a much newer Hermes may need an
update here; after `hermes update`, run the plugin tests with Hermes's own interpreter
(`~/.hermes/hermes-agent/venv/bin/python -m pytest harness/plugins`).

## Documentation

- [Architecture](docs/architecture.md) — components, event flow, waits and rechecks
- [Configuration](docs/configuration.md) — `herdr-pipeline.json`, profile settings, project notes
- [Manual install](docs/manual-install.md) — the installer's steps as commands
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)

## License

[MIT](LICENSE)
