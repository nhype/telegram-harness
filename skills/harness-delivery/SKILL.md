---
name: harness-delivery
description: Use when the owner asks in chat to build, fix or change something in {{PROJECT}}, or asks how the agents are doing.
version: 1.0.0
author: telegram-harness
license: MIT
metadata:
  hermes:
    tags: [herdr, openspec, claude-code, telegram, delivery]
    related_skills: [harness-controller]
---

# {{PROJECT}} delivery through OpenSpec + Herdr

Scope: {{PROJECT}}, repo `{{REPO}}`. "Build/fix X" = run the full cycle through a Claude Code agent in a
Herdr pane until the change is live, verified, pushed and archived. Hermes is the manager: it launches,
routes, verifies and reports. It does not write product code itself.

## Standing owner rules (never ask again)

- The full cycle is authorized: Propose → Apply → tests → deploy → live check → commit/push →
  sync/archive → final commit/push → close the agent. Registry policy flags default to all `true`.
- Ask the owner ONLY for: user-visible product decisions the request does not settle, money beyond the
  approved budget, destructive or irreversible production actions, the owner's personal accounts.
  Technical, internal and reversible choices are decided by Hermes and reported, never asked. A question
  never stops the whole task: the rest continues.
- One question at a time: context → options → recommendation → one question. Never a bare numbered list.
- Never postpone checks to "tomorrow"; prove behavior now with a smoke or a test account.
- Reports: the owner's language, plain words, ≤6 lines, no IDs/SHAs/jargon.

## 1. Start (one chat turn, ≤10 tool calls)

1. Classify the task (when unsure pick the higher one):
   - `micro`: ≤4 static/UI/copy files, no backend/DB/money/auth/privacy/queues. Skip reviews.
   - `standard`: contained backend or UI behavior, one API/read-model, no migration.
   - `high-risk`: payments, migrations, privacy/auth, queues/retry, delivery state, multi-service.
2. Pick the pane: reuse the task's pane for a continuing change; otherwise create one:
   `herdr_control tab_create` (cwd `{{REPO}}`) → **register first**: `herdr_task create`
   (id `{{PROFILE}}-<change>`, pane ids, cwd, change, phase `propose`, execution_profile, risk_flags,
   brief per «Brief») → `herdr_control pane_run text="claude --dangerously-skip-permissions"` → answer
   the startup trust screen if shown. Model/effort come from the repo's `.claude/settings.json`.
3. Send the first prompt («New task» template) with `herdr_control agent_prompt` (wait=false) and confirm
   the returned status is `working`.
4. Tell the owner in one or two lines what was started. From here the event controller
   (`harness-controller`) drives the phases; do not poll in chat turns.

## 2. Phases

| Phase | Prompt essence | Gate |
|---|---|---|
| Propose | `/opsx:propose <owner request + acceptance criteria>`; compact artifacts, no code | plan review (standard/high-risk) |
| Apply | after `herdr_control fresh_session`: `/opsx:apply <change>`; read artifacts + handoff.md first | tests green |
| Pre-deploy review | reviewer pane: review the diff for HIGH/MEDIUM risks only | blocking findings fixed |
| Deploy + live check | deploy per the repo's docs, then a real smoke | behavior proven live |
| Publish | commit + push, verify `origin/main` == HEAD | remote verified |
| Finalize | `/opsx:sync` + `/opsx:archive <change>`, commit + push | clean tree |
| Close | `/exit`, pane close, `herdr_task update completed=true` | — |

A fresh session is mandatory only between Propose and Apply (planning context must not leak into
implementation). Everything after Apply continues in the same session unless context runs out.

## 3. Reviews (cheap, bounded)

- An independent reviewer is a separate Claude Code pane registered with `parent_task_id` = the author
  task, with read-only scope (no product writes, no deploy, no spend).
- At most two gates (plan, pre-deploy diff). Blocking only HIGH/MEDIUM in money, privacy, security, data
  loss, concurrency, wrong behavior. LOW/wording → the author fixes them, no new round.
- A re-review checks only the listed fixes. The reviewer writes its verdict to a file and returns the path
  and a ≤10-line summary.

## 4. Prompts to agents

An agent needs a task, not a protocol. Every sentence must change what the agent does.

**In (in order, only what is needed):** goal in the owner's words; done-when (2–6 checkable points);
constraints as facts ("budget for paid calls: up to $5 total", "do not touch X"); now — one action ("plan
only, no code" / "fix, deploy, check live" / "stop before deploy — there will be a review"); context paths;
reply format ("≤10 lines: what was done, tests in one line, what is deployed and verified, what is left").

**Never:** who/when approved, message/session/pane IDs, policy flags, gate IDs, receipts, Hermes
internals (registry, generations, event IDs), disclaimers, repo rules that live in CLAUDE.md/AGENTS.md,
"no push/deploy/…" lists, edits outside the repo.

An owner decision becomes a fact: "the owner allowed up to $5" → "Budget for paid calls: up to $5 total".

### Templates

New task (Propose):
```
/opsx:propose <change>
Goal: <in the owner's words>.
Done when: <2–6 checkable points>.
Constraints: <budget, what not to touch>.
Now: only the OpenSpec artifacts and a short handoff.md, no code changes.
Open product questions — one list at the end, each with your recommendation.
Reply in ≤10 lines: plan summary, risks, questions.
```
Continue after idle: `Continue <change>: left <tasks/step>. Now: <step>. Reply in ≤10 lines.`
After an API error or cut-off: `Continue from where you stopped (the reply was cut off by a connection
error).` Add "First check that your last edits were saved" if it stopped mid-edit.
Owner's answer to an agent question:
```
Answer to your question about <topic>: <decision as a fact>.
Continue: <next step>.
```
Review request (reviewer pane):
```
Review <change>: <what was done, one sentence>.
Changes: <base..head or files>; description — openspec/changes/<change>/handoff.md.
Look only for blockers: money, privacy, security, data loss, concurrency, wrong behavior.
Do not change the repo or deploy; running tests is fine.
Report to <path>; reply in ≤10 lines: PASS or the problems (where, how to reproduce, what is expected).
```
Finalize: `Finish <change>: commit + push, /opsx:sync, /opsx:archive <change>, commit + push. Done when
HEAD == origin/main, the change is archived and you have no uncommitted edits. Reply in ≤5 lines.`

### Brief

`brief` in the registry is the task for the agent, not a log: `Goal: … Done when: … Constraints: …
Context: <paths>.` About 1000 characters (max 2000). An owner decision that changes a constraint →
rewrite the Constraints line and add one `herdr_task decision`. Never put model, IDs, flags, phase or
status in the brief (those live in `phase`, `wait`, `notes`).

## 5. Owner messages during work

- Controller questions reach the owner from webhook runs this chat never saw. When agents wait on the
  owner, the owner's turn carries a block «Herdr: agents are waiting on the owner for these questions».
  A short reply ("yes", "no", "2", "go ahead") answers the listed question (the newest one if several; if
  it is really ambiguous, ask which in one line).
- When the owner answers a pending question: send the answer to the pane immediately (`agent_prompt`,
  «Owner's answer» template — the decision as a fact), check it is `working`, `herdr_task decision`
  (1 line, by `user`) and clear `wait`. Only then say "sent".
- Status request ("how is it going", "status"): `herdr_task list` + `herdr_inspect agent_get` per active
  pane, then ≤6 lines: what already works, what is left, whether agents work or wait, what is needed from
  the owner, what the timing depends on. If a pane is idle with unfinished work and no wait — fix it right
  now (send the next step), say so, and add one `herdr_task note` "stall: <cause>".
- Delayed check: `herdr_task update wait={reason:"scheduled", until:<UTC>}` — the bridge wakes the
  controller. Do not create cron jobs for task follow-ups.

## 6. Safety

- Explicit session/pane IDs only; never UI focus. Never touch panes outside `{{REPO}}` and its worktrees.
- Never `git reset --hard`, force-push, or delete unrelated files to get a clean tree.
- Never reveal secrets from `.env`, transcripts or terminal history.
- Destructive production/database operations and new spending need one explicit owner confirmation.
