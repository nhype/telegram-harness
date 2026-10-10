# Configuration

## `~/.hermes/profiles/<profile>/herdr-pipeline.json`

Written by the installer (mode 0600). Every harness component reads its project settings from here;
without a valid file the plugins and the route script stay inactive for that profile.

| Key | Example | Meaning |
|---|---|---|
| `profile` | `"acme"` | Hermes profile name. |
| `project` | `"Acme"` | Project name. Tasks are matched by it exactly (the registry forces it on create). |
| `cwd_prefixes` | `["/srv/acme"]` | Absolute repo paths. Only panes whose cwd is inside one of them are routed. |
| `sibling_prefix` | `true` | Also route sibling dirs such as `/srv/acme-worktrees/x` (`--sibling`). |
| `route` | `"herdr-acme"` | Webhook route name on the host gateway. |
| `chat_id` | `"123456789"` | Telegram chat that receives controller messages and approval prompts. |
| `owner_user_id` | `"123456789"` | The only Telegram user whose approvals and answers count. |
| `approval_tag` | `"[Acme Herdr approval]"` | Prefix of approval prompts. |

See [`templates/herdr-pipeline.json.example`](../templates/herdr-pipeline.json.example).

## What the installer sets

On the profile (`hermes -p <profile> …`):

- `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS=<owner>`, `TELEGRAM_HOME_CHANNEL=<owner>`.
- `approvals.mode: smart`, `approvals.timeout: 300`, and `approvals.smart_policy` from
  [`templates/smart-policy.txt`](../templates/smart-policy.txt) unless you already have one.
- Plugins enabled: `herdr-control`, `herdr-approval-bridge`, `herdr-silence-fix` (symlinks into this repo).
- Toolsets: `herdr` for Telegram; `file herdr skills terminal` for webhook (controller) runs.
- `scripts/` → a symlink to `harness/scripts` (the route script lives there).
- Skills `harness-controller` and `harness-delivery`, rendered with your project name and repo path.

On the host (default) profile:

- `platforms.webhook.enabled: true`, listening on `127.0.0.1:<port>` (first free port from 8650 unless
  one is configured already). Each route carries its own HMAC secret in Hermes's subscriptions file.
- The route `herdr-<profile>`, bound to your profile (`--route-profile`), delivering to your Telegram chat.

## Project notes

The controller skill ends with a section you fill in:

```
## Project notes (edit me)
- Production: how it is deployed
- Smoke / live check: one command that proves a change live
- Test accounts or keys the controller may use for live checks
- Never touch: paths, services or data the agents must not change
```

Edit `~/.hermes/profiles/<profile>/skills/harness-controller/SKILL.md`. The installer keeps an edited
skill as it is on every re-run (it tracks a hash of the rendered copy in `.harness-rendered`).

## Model and effort of the coding agents

The installer copies [`templates/claude-settings.json`](../templates/claude-settings.json) to
`<repo>/.claude/settings.json` when the repo has none. Change `model` / `effortLevel` there; the
controller never switches models itself.

Agents are launched as `claude --dangerously-skip-permissions --settings <profile>/claude-agent-settings.json`.
That file (from [`templates/claude-agent-settings.json`](../templates/claude-agent-settings.json), kept once
edited) makes long sessions compact at 400k tokens (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`) instead of growing
toward the full window, and turns off prompt suggestions, which a stray Enter from the controller could
submit as a task. It only affects sessions started after a change.

## Several projects

Run the installer once per project with a different `--project`/`--profile`. Each profile gets its own
bot (use a separate bot token per project), route, bridge unit and registry; they share the host gateway
and the Herdr server.
