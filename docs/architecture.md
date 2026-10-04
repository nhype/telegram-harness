# Architecture

## Pieces

| Piece | Where | What it does |
|---|---|---|
| Herdr server | `herdr server` (systemd unit `herdr-server.service` if the installer created it) | Runs agent panes, exposes a socket API and status events. Herdr's Claude Code integration (`herdr integration install claude`) reports each agent's session id, which the controller uses to verify a fresh session before Apply. |
| Bridge | `harness/scripts/herdr_event_bridge.py`, unit `harness-bridge-<profile>.service` | Subscribes to Herdr status events for panes whose cwd is inside the configured repos, debounces them and delivers one webhook per event to Hermes. |
| Task registry | `<profile>/state/herdr_tasks.json` (+ `herdr_registry.py`) | One record per task: pane, cwd, OpenSpec change, phase, policy, wait, notes, decisions. Written under a lock, validated like the bridge reads it. |
| Route script | `harness/scripts/herdr_workflow_context.py` (linked as `<profile>/scripts/`) | Runs inside Hermes for every webhook: drops events of other projects or unknown tasks, and attaches the workflow (author + reviewer) from the registry. |
| Hermes host gateway | unit `hermes-gateway.service` | One gateway per host serves every Hermes profile: the profile's Telegram bot and its webhook route `herdr-<profile>`. |
| Plugin `herdr-control` | `harness/plugins/herdr-control` | Tools `herdr_inspect`, `herdr_control`, `herdr_destructive`, `herdr_task`; a hook that shows the owner's chat turn which questions agents are waiting on. |
| Plugin `herdr-approval-bridge` | `harness/plugins/herdr-approval-bridge` | Sends command-approval prompts from controller runs to the owner and binds the reply (`/approve`, `/deny`) to that exact request. |
| Plugin `herdr-silence-fix` | `harness/plugins/herdr-silence-fix` | Treats controller turns as machinery so a `[SILENT]` answer is never delivered to Telegram. |
| Skills | `<profile>/skills/harness-controller`, `harness-delivery` | How the controller handles one event; how the chat side starts tasks and answers the owner. |

## Event flow

1. You write a task in Telegram. The chat run (skill `harness-delivery`) creates a Herdr pane in your
   repo, registers the task (`herdr_task create`), starts `claude` and sends the first prompt.
2. The agent works. Herdr reports `working`, then `idle` or `done`.
3. The bridge waits 20 s (idle/done) so a flapping status does not wake anyone, then delivers the event:
   `hermes webhook test herdr-<profile> --payload …` on the host gateway.
4. Hermes runs the route script in the profile's scope. Unknown task, other project, or a cwd outside the
   configured repos → the event is ignored.
5. A controller run (skill `harness-controller`) reads the pane once, decides by its table, sends one
   prompt, records one registry update and answers `[SILENT]` unless you need to know something.
6. Repeat until the change is deployed, verified, archived and merged; the controller then sends `/exit`
   and closes the pane.

## Guarantees the controller relies on

- **One controller per workflow.** Events that arrive while a controller runs for the same author/reviewer
  pair are deferred and re-evaluated after it finishes.
- **Watchdog.** A task idle ≥ 15 min without a declared wait wakes the controller with `reason=stall`; an
  elapsed wait with `reason=wait_elapsed`; a working pane with no output for 20 min with
  `reason=working_no_output`.
- **Waits always end.** Every wait has an `until`. The registry adds one when the controller forgets:
  3 h for a question to the owner (`user_decision`), 1 h for an author waiting on something external.
  On that recheck the controller applies your answer, proceeds with its recommendation when the choice is
  reversible, or reminds you once.
- **Owner alerts.** The bridge messages the owner itself, through the profile's Telegram bot and not
  through Hermes, in two cases: events are being lost to delivery failures (once an hour), or a task is
  still idle 90 min after two stall wakes did not move it (once per episode). Silence then means the
  pipeline works.
- **Quiet waits** (`quiet: true`) hold the agent's short progress turns until `until`; they are only for
  automated runs that keep posting progress by themselves.

## Owner questions

The controller decides technical, internal and reversible questions itself and records the decision. It
asks you only about money beyond the budget, irreversible production actions, user-visible product
choices and your personal accounts — and asking never stops the rest of the task. Your reply in the chat
is routed to the waiting task: the `herdr-control` hook shows the chat run the open questions, so a bare
"yes" lands where it belongs.
