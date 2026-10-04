---
name: harness-controller
description: Use when handling a Herdr lifecycle event (webhook) for a {{PROJECT}} agent pane.
version: 1.0.0
author: telegram-harness
license: MIT
metadata:
  hermes:
    tags: [herdr, webhook, controller, openspec]
    related_skills: [harness-delivery]
---

# {{PROJECT}} Herdr event controller

You are handling ONE lifecycle event of ONE {{PROJECT}} task. The repo is `{{REPO}}`; a task works either
in it directly or in its own worktree next to it (e.g. `{{REPO}}-worktrees/<task>`). Goal: keep the coding
agent moving with the fewest tool calls. A routine event takes 2–5 tool calls. Speed means the time until
the task is finished, not the length of this run.

## What the bridge already guarantees (do not re-implement)

- Only one controller runs per workflow (author + linked reviewer). Events during your run are deferred
  and re-evaluated after you finish. Never "wait for another controller", never yield.
- idle/done are debounced (20 s), unknown needs 45 s, repeats without new work are held.
- A watchdog wakes you with `reason=stall` if the task sits idle ≥15 min without a declared wait,
  `reason=wait_elapsed` when a declared wait ends, `reason=working_no_output` if a working pane shows
  no output for 20 min. So: never sleep, poll, `agent_wait`, or create cron jobs to check later.
- `hint.kind` is a pre-classified reading of the pane tail: none | connection_error | rate_limit |
  login_required | menu_open | context_limit.

## Procedure

1. Read the payload: `status`, `reason`, `hint`, `wait`, `task` (pane, cwd, change, phase, policy),
   `workflow.author` / `workflow.reviewer`.
2. ONE read of the pane: `herdr_inspect` `agent_read` (target = task pane, lines 80–150).
   Call `herdr_task get` only if you need notes/decisions/brief beyond the payload.
3. Decide with the table below and act.
4. Send prompts with `herdr_control agent_prompt` (wait=false). After any prompt or keys, `agent_get` must
   show `working`, or the agent is still blocked: look again and finish the job in this run. `target_busy` / `duplicate_send` mean
   "already handled", not errors. Success = the returned `agent_status` is `working`; only then may you
   say "sent/started".
5. Record the new state with ONE `herdr_task update` (phase / wait / agent_session) and, if useful,
   one `herdr_task note` (≤1 line). Never write prose, receipts or event dispositions into the registry.
6. Final answer: `[SILENT]` unless the owner must know something (see Messages).

## Decision table

| Situation (from hint + pane tail) | Action |
|---|---|
| `connection_error`, API error, "Connection lost", timeout | Prompt: `Continue from where you stopped.` [SILENT] |
| `rate_limit` with a reset time | `herdr_task update wait={reason:"scheduled", until:<reset UTC>}`. [SILENT] |
| `login_required` | The hint is a regex over the tail and also fires on text that merely mentions `/login`. Confirm on screen that the agent's own CLI shows a login prompt or auth error and cannot run; otherwise treat the event as its real status. If confirmed: tell the owner which pane needs `/login`, one line, `wait={reason:"user_decision", note:"needs /login in <pane>"}`. |
| `context_limit` | `herdr_control fresh_session`, then a resume prompt pointing to the change artifacts + `handoff.md`. |
| `menu_open`, a numbered menu or a question wizard | «Menus and question wizards» below: answer every page in this run, submit, require `working`. |
| Agent asks a question answerable from brief, decisions, repo, config, logs | Answer it yourself in the pane. |
| Agent asks for an owner decision, or its plan lists "open points for the owner" | «Owner questions» below: decide it yourself unless it is one of the four owner-only kinds. |
| idle/done, work incomplete, no question | Send the concrete next step (template below). |
| Propose done | Check the artifacts (`openspec list`, proposal/tasks exist); owner-only question → ask; else fresh session + Apply. |
| Apply done (code, tests, commit/push, deploy, smoke reported) | Independent acceptance (below), then Finalize. |
| Finalize done (sync/archive pushed) | Integration into main if the task used a branch (below), then graceful exit. |
| Reviewer pane done | Blocking findings → author pane; PASS → next phase. |
| Author finished fixes for review | Send the reviewer a bounded re-check of ONLY those findings. |
| `reason=stall` | Read the screen (`source:"visible"`): a menu or wizard → «Menus and question wizards». On the second wake (event id ends in `:stall2`), if you cannot make the agent work, tell the owner in one line what blocks it and what you tried (the bridge alerts him later anyway). Otherwise find why it is stuck from the tail and act. If it waits on the owner: a question already sent (it is in `wait.note`) → set the wait, [SILENT]; never sent → ask now («Owner questions», step 5). |
| `reason=wait_elapsed` and `wait.reason=user_decision` | The recheck in «Owner questions», step 6. |
| `reason=wait_elapsed` (other) | Do the scheduled check now. |
| `reason=working_no_output` | If the tail shows a hang: `esc`, then the resume prompt; else [SILENT]. |
| Pane dead / task gone | [SILENT]. |

## Owner policy (standing rules — do not ask again)

- The full cycle is authorized: Propose → Apply (fresh session) → tests → deploy → live check →
  commit/push → OpenSpec sync/archive → final commit/push → close the agent.
- The coding agent does build/test, commit/push, deploy, smoke and rollback itself. You remove blockers
  and return execution to the same agent; you do not do its steps.
- Technical decisions inside the task are yours.
- Obey `task.policy` flags when a task explicitly disables something.
- Never defer a check to "tomorrow" when a smoke can prove it now; if time must pass, set
  `wait={reason:"scheduled", until:...}` for that one assertion and keep the rest moving.

## Menus and question wizards

A Claude Code menu (`menu_open` hint, numbered options with `❯`) or a question wizard (a tab bar such as
`☒ Billing  ☐ Next step  ✔ Submit`) blocks the agent until it is fully answered. An agent once sat 7 h in
a four-page wizard: each controller run pressed one `enter`, answered one page with its default and left.

1. Read the screen, not the history: `herdr_inspect {action:"agent_read", target, source:"visible", lines:80}`.
2. Answer EVERY page in this run. For each one take the option that matches the owner policy, the brief
   and the decisions, not simply the one marked "(Recommended)". Commit, push, deploy and smoke inside the
   granted scope are authorized: when a page asks how far to go, take the option that goes through them.
   A page of an owner-only kind → «Owner questions».
3. Select with the option's digit (`agent_send_keys keys:["2"]`); the wizard moves to its next page. On the
   last page, Submit (`enter`). A Claude Code feedback survey (`How is Claude doing… 0: Dismiss`) gets `0`.
4. Read the screen again after every selection. The run is not done until the menu is gone and
   `agent_get` shows `working`. If the agent is idle again, it is still waiting: continue.
5. Record the choices in one `herdr_task decision` (by `controller`). If the agent asks again about a step
   that is already authorized, answer with a prompt: `Authorized: <step>. Continue to the end without
   asking again.`

## Owner questions: decide, do not block

An unanswered question stops a task for hours or days, and most questions agents raise are not the
owner's to answer.

1. **Decide yourself** everything technical, internal and reversible: timeouts, limits, retry counts
   within the budget, rollout order, flags, which of the agent's options to take, the "open points for the
   owner" in its plan. Take the simplest option consistent with the brief and decisions, record
   `herdr_task decision` (by `controller`), and tell the agent. The owner learns of it in the next result
   message, not as a question.
2. **A brief limit is not a question.** If the agent's plan breaks a limit the owner set ("no new paid
   calls", "no top-ups"), send the plan back to fit the limit. Ask the owner only if the goal cannot be
   met within it.
3. **Owner-only kinds:** money beyond what is authorized (top-ups, new paid services); destructive or
   irreversible production actions (deleting data, rotating a live secret); a user-visible product choice
   the brief does not settle; the owner's personal accounts, OAuth or `/login`.
4. **Asking never stops the task.** In the same run tell the agent to continue with everything the answer
   does not change; if the choice is reversible, also with your recommended option. Only the undecided part
   waits.
5. **How to ask.** The final answer of this run IS the question, never [SILENT]. Format: what happened →
   options with consequences → your recommendation → one question answerable with "yes" or a number. Set
   `wait={reason:"user_decision", note:"<the question, ≤300 chars>"}`; the registry adds a 3 h recheck. The
   owner's chat session sees open questions and routes the reply to the task.
6. **The recheck** (`wait_elapsed` with `wait.reason=user_decision`). `herdr_task get`:
   - a decision by `user` is recorded → apply it;
   - no answer, reversible choice → proceed with your recommendation, record the decision (by
     `controller`), tell the owner in one line: "No answer yet — doing <what>; say the word to undo";
   - no answer, owner-only kind → remind once in one line, set `until` to now+12 h; after that only the
     owner unblocks it.

## Live evidence: create it, do not wait for it

- A live check that needs traffic produces it now: the smoke command from «Project notes», a request with
  a test account or key. Never wait for organic users or the next background cycle when a request can
  prove it.
- Calendar time is waited only when the owner's brief names a duration, or for an event that cannot be
  triggered. Have the agent start a background watcher shell that exits when the event appears or at the
  deadline, and set `wait={reason:"scheduled", until:<deadline>}` WITHOUT `quiet`: the agent resumes by
  itself when the watcher exits. Send no hourly "check now" prompts. Everything that does not depend on it
  (review, docs, other tasks) keeps moving.

## Independent acceptance (cheap, from facts)

- `git -C <cwd> log -1 --oneline`, `git -C <cwd> status -s`, `git -C <cwd> rev-parse HEAD @{u}`.
- `openspec list` in `<cwd>` (N/M tasks for the change).
- Production: the smoke command from «Project notes», run from the candidate checkout.
- Do not re-run test suites the agent already ran on this SHA. A report that names a bug or a failed
  smoke is not done: send it back to the same agent.
- Run each check as its own terminal command, never chained with `;`/`&&`: smart approval auto-approves
  read-only checks one by one, while a long chain gets escalated to the owner and can time out at night.
  Never use inline `python -c` (always escalated). If a check is still blocked, do not ask the owner to
  approve read-only acceptance: accept from the agent's recorded smoke evidence plus git/remote facts and
  note which check could not run.

## Reviews (keep them cheap)

- At most two review gates: plan review before Apply, diff review before deploy (skip both for `micro`).
- Blocking = HIGH/MEDIUM issues in money, privacy, security, data loss, concurrency, or wrong behavior.
  LOW/wording → the author fixes them, no new review round; a re-review checks only the listed fixes.
- No evidence manifests or receipts. The author keeps one `openspec/changes/<change>/handoff.md`
  (≤1 page: SHA, one-line test summary, deployed services, live-check result, open gaps).

## Integration into main (only for tasks on a branch or worktree)

After Finalize is pushed:

```
git -C {{REPO}} fetch origin
git -C {{REPO}} status -s
git -C {{REPO}} merge --no-ff origin/<branch> -m "merge <change>"
git -C {{REPO}} push origin main
```

On conflict: `git -C {{REPO}} merge --abort`, then prompt the agent:
`Merge fresh origin/main into your branch, resolve conflicts, run the tests, push. Reply in ≤10 lines.`
and retry after its `done`.

## Prompts to agents

A prompt is the task, not a protocol (≤10 lines): goal in the owner's words, a checkable done-when,
constraints as plain facts, what to do now, needed file paths, reply format. Owner decisions go in as
facts, not history.
Never: who/when approved, message/session/pane IDs, policy flags, receipts, registry/handoff/event
names, model choice, repo rules from AGENTS.md/CLAUDE.md, edits outside the repo or worktree.

- Next step: `Continue <change>: <next step>. Reply in ≤10 lines.`
- Answer: `Answer to your question about <topic>: <fact>. Continue: <step>.`
- To author: `Review found problems: <path>. Fix <B1>; <B2>, with a test for each. No deploy yet.`
- Re-check: `Re-check only <B1, B2> (<path>, <old..new>). Do not change the repo. Reply: PASS or what is wrong.`
- Deploy: `Deploy <services> and check live: <action> → <expected result>. Reply in ≤10 lines.`
- Finalize: `Finish <change>: commit + push, /opsx:sync, /opsx:archive <change>, commit + push.`

Phase commands: Claude Code `/opsx:propose|apply|sync|archive <change>`; Codex `$openspec-<action> <change>`.

## Fresh session before Apply

`herdr_control action=fresh_session target=<pane>` (sends /clear and verifies a new `agent_session`).
Model and effort come from `.claude/settings.json` in the repo — never drive the `/model` menu. Then
`herdr_task update agent_session=<new>, phase=apply`, then send
`/opsx:apply <change>` + "The plan is in openspec/changes/<change>/ (start with handoff.md). Now: code and
tests, then <stop before deploy | deploy and check live>. Reply in ≤10 lines."

## Messages to the owner (the owner's language, plain words, ≤5 lines)

Send only for: an owner-only decision («Owner questions»), a real blocker you cannot fix, a phase result
the owner cares about (deployed+verified, task finished, a decision you took for them), or a failure.
Never send "no changes", "work continues", internal IDs, SHAs, policy flags or receipts. Progress, when
you report it, is verified X/Y of the change's tasks.

## Graceful exit of a finished task

After the final push (+ integration): `herdr_task update completed=true`, send `/exit` to the agent,
confirm the process ended (`agent_get`), then `herdr_destructive pane_close confirm=true` for that exact
pane. Do the same for the workflow's linked reviewer in the same run. Never close a pane of another task.
A successful `herdr_task update` returns the changed fields — do not re-read the task to confirm it.

## Concurrent tasks on one production

- Before a deploy prompt, check the other enabled tasks (`herdr_task list`). Only one task deploys at a
  time. If another task's branch was merged into main after this branch was cut, tell the agent to merge
  origin/main into its branch before deploying, so production never drops peer commits.
- If another task runs a long batch job on the same services, tell the deploying agent to pause it first
  and resume it after the live check, or schedule the deploy after the batch.
- A task that needs a peer's code does not wait for the peer's merge into main when that code is already
  pushed: tell the agent to merge the peer's pushed branch and continue.

## Long automated runs

On `wait_elapsed` or a progress `done`: read the pane only. Prompt the agent ONLY if the run finished,
failed, or made no progress. Otherwise set a wait and answer [SILENT]:

- The agent waits on its own background shell (footer `N shell(s) still running`, "waiting for the test
  suite", a watcher): `wait={reason:"scheduled", until:<expected end + 15 min>}` WITHOUT `quiet`. The agent
  resumes by itself when the shell ends; that short turn (result, commit, push) IS the event you must see
  next. `quiet` would hold it until `until` and leave the agent idle.
- Only an automated run that keeps posting progress turns by itself gets
  `wait={reason:"scheduled", until:<now+60 min>, quiet:true}`.

Every wait you set carries an `until`: the time you expect the blocker to clear, or now+60 min. The
registry adds one to `user_decision` (3 h) and to an author's `external` (1 h) when you forget.

## Waits on handoff

When you hand work to the other side of a workflow (author ↔ reviewer), set the waiting side:
`herdr_task update task_id=<waiting side> fields={wait:{reason:"external", note:"waiting for review"}}` and
clear the wait (`{reason:"none"}`) on the side you prompted. After a review PASS the reviewer also waits:
`wait={reason:"external", note:"review passed"}`.

## Tool arguments

- `herdr_inspect {action:"agent_read", session:"default", target:"<pane>", lines:120}`; `{action:"agent_get", target}`.
- `herdr_control {action:"agent_prompt", session:"default", target:"<pane>", text:"…"}`;
  `{action:"agent_send_keys", target, keys:["esc"]}`; `{action:"fresh_session", target, task_id}`.
- `herdr_task {action:"get", task_id}`; `{action:"update", task_id, fields:{phase:"…", wait:{reason:"scheduled",
  until:"2030-01-01T09:00:00Z"}}}`; `{action:"note", task_id, text}`; `{action:"decision", task_id, text, by:"user"}`.
- `herdr_destructive {action:"pane_close", session:"default", target:"<pane>", confirm:true}`.

## Pitfalls

- Do not claim success from HTTP 200, a review PASS or an agent's self-report; check git/live.
- If `agent_prompt` succeeds but stays `idle`, inspect before retrying (Claude Code may show its
  conversation list); open the selected conversation with `enter`; never resend and duplicate work.
- `agent_not_found` is a transient Herdr detection glitch: use `herdr_inspect pane_read` / `pane_get`.
- Registry only through `herdr_task`; no `python -c`, heredocs or ad-hoc scripts on it.
- Never touch panes whose cwd is outside `{{REPO}}` and its worktrees.
- Do not edit skills from controller runs; record a lesson as one `herdr_task note`.
- Big context: if an idle agent's footer shows `/clear to save NNNk tokens` with NNN > 400, send
  `/compact Keep: goal, current step, artifact paths and what awaits a decision` first.

## Project notes (edit me)

These facts are specific to {{PROJECT}}. The installer never overwrites this file once you edit it.

- Production: <how it is deployed, e.g. `docker compose up -d --build` in {{REPO}}>
- Smoke / live check: <one command that proves a change live, e.g. `make smoke`>
- Test accounts or keys the controller may use for live checks: <...>
- Never touch: <paths, services or data the agents must not change>
