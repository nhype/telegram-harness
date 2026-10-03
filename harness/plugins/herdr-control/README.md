# Herdr Control Bridge

Profile-local Hermes plugin that exposes Herdr control to the `demo` Telegram gateway through explicit named sessions and opaque Herdr IDs.

## Telegram usage

Natural-language examples:

- `Show the Herdr sessions and agents.`
- `Read the last 100 lines of the reviewer agent in session default.`
- `Create a workspace for /srv/project and start Codex there.`
- `Tell the reviewer: check the current diff, then wait for the result.`
- `Show why the agent is blocked; approve nothing.`

Read-only shortcut:

```text
/herdr status [session]
/herdr sessions
/herdr workspaces [session]
/herdr tabs [session]
/herdr panes [session]
/herdr agents [session]
```

## Tools

- `herdr_inspect`: status/list/get/read/explain/wait; read-only.
- `herdr_control`: explicit create/split/run/start/prompt/input operations, plus `fresh_session`.
- `herdr_destructive`: close/stop/delete; requires `confirm=true` after exact-scope user confirmation.
- `herdr_task`: locked, validated access to `state/herdr_tasks.json` through
  `scripts/herdr_registry.py`. Actions: `get`, `list`, `update`, `note`, `decision`, `create`, `archive`.
  Controllers use this tool to change the registry. They do not edit the JSON with patch, write_file or the terminal.

### Send guards (`agent_prompt`, `pane_send_text` to an agent pane)

- `agent_prompt` does not wait by default (`wait: false`). Use `herdr_inspect agent_wait` when you need to block.
- Before sending, the tool runs `herdr agent get` on the target. The tool refuses to send, and sends nothing, when:
  - the target is `working`: `target_busy`
  - a prompt is being sent to the target now, or a prompt went out less than 5 s ago: `target_busy` with `reason: send_in_flight` or `reason: recent_send`
  - `agent_prompt` or `fresh_session` targets a `blocked` agent: `target_blocked`
  - the same text (sha256) already went to the same agent session less than 10 minutes ago: `duplicate_send`
- `force: true` skips these checks. Use it only after you have inspected the target.
- `pane_send_text` shorter than 16 characters is treated as a keystroke, such as a menu digit or y/n. It skips the duplicate check and the recent-send window. The busy check still applies.
- A successful send returns `post_send` with `agent_status`, `revision` and `agent_session`, taken from one quick `agent get` with no waiting.
- The send journal is `state/herdr_sends.json`, protected by the lock `herdr_sends.lock`. It keeps the last 200 entries and stores only hashes and lengths, never the text.

### `fresh_session`

`fresh_session` needs a target Claude Code pane and accepts an optional `task_id`. It sends `/clear`, then runs `agent get` every 1 s for up to 20 s until
`agent_session.value` changes and the agent is idle or done. It returns `{old_session, new_session, changed}`.
If the session does not change, it returns `error: session_not_reset`. With `task_id`, it also stores the new
`agent_session` and `previous_agent_session` in the registry. The model and effort are pinned by the project's
`.claude/settings.json`, so the tool does not touch the `/model` menu.

### Keys

`keys` is an enum of canonical herdr names: `enter`, `esc`, `tab`, `backtab`, arrows, `pageup`, `pagedown`, `backspace`,
`delete`, `space`, `ctrl+<letter>`, `0`–`9`, `y` and `n`. Common aliases are normalized case-insensitively, for example `CTRL_U`, `C-u`
and `Ctrl-U` become `ctrl+u`, `ARROWUP` becomes `up`, `RETURN` becomes `enter`, and `ESCAPE` becomes `esc`. herdr rejects `home` and `end`, so use `ctrl+a` and `ctrl+e`.

### Task registry

- Every write takes an fcntl lock (`herdr_tasks.lock`), validates with the event-bridge rules and then with
  `herdr_event_bridge.load_tasks` when that module can be imported, and replaces the file atomically with mode 0600.
- Only whitelisted keys are accepted. Unknown keys are rejected with the list of allowed keys.
- `policy` merges partially. A partial update gives any missing key the bridge default, so it cannot grant new permissions.
- `phase_generation: "+1"` increments the value under the lock. `expect_generation` gives compare-and-set.
- Structured fields replace free-text dispositions:
  - `wait {reason, until, note}`
  - `notes` (max 20)
  - `decisions` (max 20 per task, and max 50 global through `task_id: "*"`)
- `archive` appends the full task to `state/herdr_tasks_archive.jsonl` and leaves a tombstone stub:
  `enabled:false, tombstone:true`. The stub stops the bridge from reviving the live pane as `auto:<pane>`. When the pane is gone, the stub is pruned.
- One-time slimming of the registry. The first command is a dry run, and `--apply` performs the migration:
  `python3 scripts/herdr_registry.py migrate [--apply] [--live-panes pane-list.json]`.

## Safety contract

- Always pass a named session; default is `default`.
- Always use IDs returned by live Herdr JSON or a unique live agent name.
- Never infer another client's focused pane and never use `--current`.
- Inspect a blocked agent before sending input; never auto-approve permissions.
- Do not close/delete objects not created by the bridge unless the user identifies and confirms them.
- Prompt text, shell commands, and literal terminal text are redacted from the bridge's returned command metadata.
- Herdr subprocess calls use argv arrays, no shell, bounded output, and bounded timeouts.

## Installation state

- Plugin path: `~/.hermes/profiles/<profile>/plugins/herdr-control/` (a symlink into `telegram-harness/harness/plugins/`)
- Enabled plugin: `herdr-control`
- Enabled toolset on: `telegram`, `cli`
- Herdr integrations installed: `codex`, `claude`, `hermes`

## Verification

Run local tests:

```bash
python3 -m pytest -q plugins/herdr-control/tests scripts/tests/test_herdr_registry.py   # from the profile dir
```

The first E2E was performed in disposable Herdr session `hermes-bridge-test`: workspace creation, pane command/write/read, Codex start, prompt submission, lifecycle read, and response marker `HERDR_AGENT_E2E_SUCCESS`.
