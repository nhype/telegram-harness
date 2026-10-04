<p align="center">
  <img src="docs/assets/logo.svg" alt="" width="96">
</p>

<h1 align="center">telegram-harness</h1>

<p align="center">
  <b>Your coding agents, managed from Telegram.</b><br>
  Claude Code agents plan, build, test, deploy and verify. A manager keeps them moving<br>
  and asks you only what is yours to decide.
</p>

<p align="center">
  <a href="https://github.com/nhype/telegram-harness/actions/workflows/ci.yml"><img src="https://github.com/nhype/telegram-harness/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/platform-Linux%20%2B%20systemd-informational.svg" alt="Linux + systemd">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-compares">How it compares</a> ·
  <a href="docs/architecture.md">Architecture</a>
</p>

<p align="center"><sub>
  <b>English</b> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <a href="README.it.md">Italiano</a>
</sub></p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/demo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/demo-light.svg">
    <img src="docs/assets/demo-light.svg" alt="A Telegram chat on the left: the owner asks for CSV export, the bot reports plan, implementation, deploy and live check. On the right, Herdr panes: the author agent edits, tests and deploys, the reviewer agent passes the change, and the bridge log shows no stalls." width="100%">
  </picture>
</p>

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

## Why telegram-harness

Most tools let you *chat* with a coding agent from your phone, and you still have to babysit it.
telegram-harness gives you a **manager**: you say what you want, and it gets the change built, tested,
deployed, verified and merged. It writes to you only when it really needs you.

- **A manager, not a relay.** Between your messages a controller reads the agent's screen after every
  step and keeps it moving. It answers technical questions from the repo and your past decisions, picks
  menu options by your policy and recovers from API errors and full contexts.
- **The whole lifecycle, to production.** OpenSpec plan → code → tests → deploy → live smoke → archive
  → merge into `main`. "Done" means *live and verified*, not "code written".
- **It asks only what is yours to decide:** money, irreversible actions, product choices, your accounts.
  Everything else it decides and reports. A bare "yes" in the chat reaches the agent that asked.
- **Never silently stuck.** An event-driven watchdog catches stalls, elapsed waits and hung panes. If a
  task still does not move, the bridge messages you itself, even when Hermes is down.
- **A second pair of eyes.** An independent reviewer agent checks the plan and the diff for money,
  privacy, security, data-loss and concurrency risks before anything ships.
- **Your server, your subscription.** Code and production never leave your machine, and agents run on
  your own Claude plan. No per-task SaaS bill, no vendor VM. MIT licensed.
- **Watch or take over at any moment.** Every agent lives in a Herdr terminal pane you can open, read
  and type into.
- **Lean context.** lean-ctx compresses what agents read, so long tasks fit and cost less.
- **Built from real use.** Extracted from a setup that ships production changes every day. It has 320+
  tests, CI, and an integration test against a real Hermes in a clean container.

## How it compares

| | **telegram-harness** | Telegram bots for Claude Code¹ | Mobile clients² | Local orchestrators³ | Cloud coding agents⁴ |
|---|---|---|---|---|---|
| Where agents run | your server | your server | your computer | your computer | vendor cloud |
| How you drive them | Telegram, plain words | Telegram chat with a session | phone / web app | desktop TUI or board | web, IDE, Slack, GitHub |
| Who keeps the agent going between your messages | **the controller** | you | you | you | the vendor agent |
| Plan → code → deploy → live check → merge, built in | **yes** | no | no | no, you review and merge | usually ends at a pull request |
| Independent reviewer agent | **yes** | no | no | no | varies |
| Stuck tasks detected and reported | **yes** | no | notifications | no | varies |
| Interrupts you only for real decisions | **yes** | every question | every question | every question | varies |
| Ships to *your own* production | **yes** | by hand | by hand | by hand | rarely |
| Cost | your Claude plan + an LLM for Hermes | your plan | your plan | your plan | per seat or usage |
| License | MIT | mostly open source | open source | open source | proprietary |

¹ e.g. claude-code-telegram, CCBot, Claude Telegram Bot Bridge. ² e.g. Happy, Omnara.
³ e.g. Claude Squad, Vibe Kanban. ⁴ e.g. Codex cloud, Cursor background agents, GitHub Copilot coding agent, Devin.
The columns describe each category's typical setup as of October 2026. Individual projects change fast,
so check their docs.

**When something else fits better:**
- you want to pair-program live from your phone, line by line (a mobile client is simpler);
- you have no Linux server, or need macOS or Docker (not supported yet);
- your team needs shared multi-user chat (telegram-harness is built for one owner per bot).

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
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Telegram gateway, chat + controller runs | 0.21.5 (d795726f) | MIT |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | the coding agent | 2.1.288 | commercial |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | spec-driven change workflow | 1.13.1 | MIT |
| [lean-ctx](https://github.com/yvgude/lean-ctx) | context compression for agents | 3.10.2 | Apache-2.0 |

The harness itself is the bridge, the task registry, the route script, three Hermes plugins, two skills
and the installer. The plugins patch a few Hermes gateway internals, so a much newer Hermes may need an
update here; after `hermes update`, run `bin/harness test-plugins` (the plugin tests, inside Hermes's
own runtime).

## Documentation

- [Architecture](docs/architecture.md) — components, event flow, waits and rechecks
- [Configuration](docs/configuration.md) — `herdr-pipeline.json`, profile settings, project notes
- [Manual install](docs/manual-install.md) — the installer's steps as commands
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)

## License

[MIT](LICENSE)
