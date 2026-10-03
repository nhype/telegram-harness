"""Mocked-herdr tests for key normalization, send guards, fresh_session and herdr_task."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1] / "__init__.py"
STAGE = Path(__file__).resolve().parents[3]
CWD = "/srv/example-app"


def load_plugin():
    spec = importlib.util.spec_from_file_location("herdr_control_guard_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeHerdr:
    """Minimal stand-in for the herdr CLI (argv-level)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.agents: dict[str, dict] = {
            "w5:pZ": {"status": "idle", "session": "sess-1", "revision": 10},
        }
        self.panes = ["w5:pZ", "w4:p1"]
        self.prompt_rc = 0
        self.on_prompt = None  # callable(target, text)
        self.clear_after_polls: int | None = 2
        self._polls_since_clear: int | None = None

    def agent_json(self, target: str) -> str:
        info = self.agents[target]
        return json.dumps({"id": "cli:agent:get", "result": {"agent": {
            "agent": "claude", "agent_session": {"kind": "id", "value": info["session"]},
            "agent_status": info["status"], "cwd": CWD, "pane_id": target,
            "revision": info["revision"], "state_change_seq": info["revision"] // 2,
        }, "type": "agent_info"}})

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        group, verb = argv[3], argv[4]
        target = argv[5] if len(argv) > 5 else ""

        def done(rc=0, out="", err=""):
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

        if (group, verb) == ("agent", "get"):
            if target not in self.agents:
                return done(1, "", json.dumps({"error": {"code": "agent_not_found", "message": "x"}}))
            if self._polls_since_clear is not None:
                self._polls_since_clear += 1
                if self.clear_after_polls is not None and self._polls_since_clear > self.clear_after_polls:
                    self.agents[target]["session"] = "sess-2"
                    self.agents[target]["status"] = "idle"
            return done(0, self.agent_json(target))
        if (group, verb) == ("agent", "prompt"):
            if self.prompt_rc:
                return done(self.prompt_rc, "", json.dumps({"error": {"code": "agent_not_found", "message": "x"}}))
            text = argv[6]
            before = self.agent_json(target)
            self.agents[target]["revision"] += 5
            if text == "/clear":
                self._polls_since_clear = 0
            if self.on_prompt:
                self.on_prompt(target, text)
            return done(0, before)
        if (group, verb) == ("pane", "send-text"):
            return done(0, "")
        if verb == "send-keys":
            return done(0, json.dumps({"result": {"type": "ok"}}))
        if (group, verb) == ("pane", "list"):
            return done(0, json.dumps({"result": {"panes": [{"pane_id": p} for p in self.panes]}}))
        return done(0, "{}")

    def sent(self, verb: str = "prompt") -> list[list[str]]:
        return [c for c in self.calls if c[4] == verb or (verb == "send-text" and c[4] == "send-text")]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = load_plugin()
    fake = FakeHerdr()
    clock = Clock()
    monkeypatch.setattr(module.subprocess, "run", fake)
    monkeypatch.setattr(module.time, "time", clock.time)
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    monkeypatch.setattr(module, "SENDS_PATH", tmp_path / "state" / "herdr_sends.json")
    registry = tmp_path / "state" / "herdr_tasks.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"version": 1, "tasks": [{
        "id": "t1", "enabled": True, "session": "default", "workspace_id": "w5", "pane_id": "w5:pZ",
        "agent": "claude", "project": "ExampleApp", "cwd": CWD, "phase": "impl",
        "agent_session": "sess-1", "policy": {"allow_continue": True},
    }]}), encoding="utf-8")
    monkeypatch.setattr(module, "REGISTRY_PATH", registry)
    monkeypatch.setattr(module, "SCRIPTS_DIR", STAGE / "scripts")
    return module, fake, clock, registry


def call(module, fn: str, **args) -> dict:
    return json.loads(getattr(module, fn)(args))


def prompt(module, text: str = "Please implement step 3 of the plan.", **extra) -> dict:
    return call(module, "control", action="agent_prompt", session="default", target="w5:pZ", text=text, **extra)


# ---------------------------------------------------------------- keys

@pytest.mark.parametrize("raw,expected", [
    ("CTRL_U", "ctrl+u"), ("C-u", "ctrl+u"), ("Ctrl-U", "ctrl+u"), ("ctrl-u", "ctrl+u"),
    ("ctrl_u", "ctrl+u"), ("CTRL+U", "ctrl+u"), ("^U", "ctrl+u"), ("control+c", "ctrl+c"),
    ("ARROWUP", "up"), ("ArrowUp", "up"), ("arrow_up", "up"), ("Up", "up"), ("ARROWDOWN", "down"),
    ("RETURN", "enter"), ("Return", "enter"), ("ENTER", "enter"), ("ESCAPE", "esc"), ("Esc", "esc"),
    ("BACKSPACE", "backspace"), ("PgDn", "pagedown"), ("page_up", "pageup"), ("shift+tab", "backtab"),
    ("1", "1"), (9, "9"), ("Y", "y"), ("n", "n"), (" ", "space"),
])
def test_key_aliases_normalize(raw, expected) -> None:
    assert load_plugin()._normalize_key(raw) == expected


@pytest.mark.parametrize("raw,hint", [("home", "ctrl+a"), ("END", "ctrl+e"), ("HOME", "ctrl+a")])
def test_home_end_rejected_with_hint(raw, hint) -> None:
    with pytest.raises(ValueError, match=re.escape(hint)):
        load_plugin()._normalize_key(raw)


@pytest.mark.parametrize("raw", ["s", "j", "ctrl+alt+delete", "SHIFT+END", "f12", "", True, None, "cu"])
def test_unknown_keys_rejected(raw) -> None:
    with pytest.raises(ValueError):
        load_plugin()._normalize_key(raw)


def test_send_keys_passes_canonical_names(env) -> None:
    module, fake, _, _ = env
    result = call(module, "control", action="agent_send_keys", session="default", target="w5:pZ",
                  keys=["CTRL_U", "ArrowUp", "1", "RETURN"])
    assert result["success"] is True
    assert fake.calls[-1][-4:] == ["ctrl+u", "up", "1", "enter"]


def test_bad_key_error_lists_allowed_names(env) -> None:
    module, fake, _, _ = env
    result = call(module, "control", action="pane_send_keys", session="default", target="w5:pZ", keys=["s"])
    assert result["error"] == "validation_error" and "ctrl+<" in result["message"]
    assert fake.calls == []


def test_schema_exposes_allowed_keys_and_new_defaults() -> None:
    module = load_plugin()
    props = module.CONTROL_SCHEMA["parameters"]["properties"]
    enum = props["keys"]["items"]["enum"]
    assert enum == module.ALLOWED_KEYS
    assert {"ctrl+u", "enter", "esc", "up", "0", "9", "y", "n"} <= set(enum)
    assert "home" not in enum and "end" not in enum and "escape" not in enum
    assert props["wait"]["default"] is False
    assert "fresh_session" in props["action"]["enum"]
    assert module.TASK_SCHEMA["parameters"]["properties"]["action"]["enum"] == [
        "get", "list", "update", "note", "decision", "create", "archive"]


# ---------------------------------------------------------------- agent_prompt guard

def test_agent_prompt_does_not_wait_by_default_and_reports_post_send(env) -> None:
    module, fake, _, _ = env
    result = prompt(module)
    assert result["success"] is True
    sent = [c for c in fake.calls if c[4] == "prompt"][0]
    assert "--wait" not in sent
    assert result["post_send"]["revision"] == 15 and result["post_send"]["agent_status"] == "idle"
    assert result["status_before"]["agent_session"] == "sess-1"
    assert "Please implement" not in json.dumps(result)  # prompt text stays redacted


def test_agent_prompt_wait_true_still_supported(env) -> None:
    module, fake, _, _ = env
    prompt(module, wait=True, timeout_ms=5000, states=["idle"])
    sent = [c for c in fake.calls if c[4] == "prompt"][0]
    assert sent[-5:] == ["--wait", "--timeout", "5000", "--until", "idle"]


def test_busy_target_is_refused_without_sending(env) -> None:
    module, fake, _, _ = env
    fake.agents["w5:pZ"]["status"] = "working"
    result = prompt(module)
    assert result["success"] is False and result["error"] == "target_busy" and result["sent"] is False
    assert not [c for c in fake.calls if c[4] == "prompt"]
    forced = prompt(module, force=True)
    assert forced["success"] is True


def test_blocked_target_is_refused_for_prompts(env) -> None:
    module, fake, _, _ = env
    fake.agents["w5:pZ"]["status"] = "blocked"
    assert prompt(module)["error"] == "target_blocked"


def test_duplicate_prompt_refused_within_window(env) -> None:
    module, fake, clock, _ = env
    assert prompt(module)["success"] is True
    clock.now += 30
    duplicate = prompt(module)
    assert duplicate["error"] == "duplicate_send" and duplicate["age_s"] == 30.0
    assert prompt(module, text="A different follow-up prompt.")["success"] is True
    clock.now += 601
    assert prompt(module)["success"] is True  # window expired
    assert len([c for c in fake.calls if c[4] == "prompt"]) == 3


def test_duplicate_allowed_after_agent_session_changes(env) -> None:
    module, fake, clock, _ = env
    prompt(module)
    clock.now += 30
    fake.agents["w5:pZ"]["session"] = "sess-new"
    assert prompt(module)["success"] is True


def test_recent_send_blocks_concurrent_second_prompt(env) -> None:
    module, _, clock, _ = env
    prompt(module)
    clock.now += 1
    second = prompt(module, text="Another controller's prompt.")
    assert second["error"] == "target_busy" and second["reason"] == "recent_send"


def test_in_flight_reservation_blocks_other_controllers(env) -> None:
    module, _, clock, _ = env
    module.SENDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    module.SENDS_PATH.write_text(json.dumps({"version": 1, "entries": [{
        "id": "x", "ts": clock.now - 2, "hold_until": clock.now + 60, "action": "agent_prompt",
        "session": "default", "pane_id": "w5:pZ", "agent_session": "sess-1", "sha256": "0",
        "status": "reserved"}]}))
    result = prompt(module)
    assert result["error"] == "target_busy" and result["reason"] == "send_in_flight"
    clock.now += 120  # stale reservation (crashed controller) expires
    assert prompt(module)["success"] is True


def test_failed_send_does_not_count_as_duplicate(env) -> None:
    module, fake, clock, _ = env
    fake.prompt_rc = 1
    assert prompt(module)["success"] is False
    fake.prompt_rc = 0
    clock.now += 10
    assert prompt(module)["success"] is True
    statuses = [e["status"] for e in json.loads(module.SENDS_PATH.read_text())["entries"]]
    assert statuses == ["failed", "sent"]


def test_non_agent_target_for_prompt_is_refused(env) -> None:
    module, _, _, _ = env
    result = call(module, "control", action="agent_prompt", session="default", target="w4:p1", text="hello there")
    assert result["error"] == "target_not_agent"


def test_journal_is_bounded(env) -> None:
    module, _, clock, _ = env
    for i in range(205):
        clock.now += 10
        prompt(module, text=f"prompt number {i} for the bounded journal")
    entries = json.loads(module.SENDS_PATH.read_text())["entries"]
    assert len(entries) == 200 and entries[-1]["status"] == "sent"
    assert "text" not in entries[-1] and "prompt number" not in module.SENDS_PATH.read_text()


# ---------------------------------------------------------------- pane_send_text guard

def test_send_text_to_plain_pane_is_unguarded(env) -> None:
    module, fake, _, _ = env
    for _ in range(2):
        result = call(module, "control", action="pane_send_text", session="default", target="w4:p1",
                      text="echo a fairly long shell line")
        assert result["success"] is True and "post_send" not in result
    assert len([c for c in fake.calls if c[4] == "send-text"]) == 2


def test_send_text_to_working_agent_is_refused(env) -> None:
    module, fake, _, _ = env
    fake.agents["w5:pZ"]["status"] = "working"
    result = call(module, "control", action="pane_send_text", session="default", target="w5:pZ", text="1")
    assert result["error"] == "target_busy"
    assert not [c for c in fake.calls if c[4] == "send-text"]


def test_short_menu_text_is_not_duplicate_checked(env) -> None:
    module, fake, _, _ = env
    for _ in range(2):
        assert call(module, "control", action="pane_send_text", session="default",
                    target="w5:pZ", text="1")["success"] is True
    long_text = "a long pasted prompt text for the agent"
    assert call(module, "control", action="pane_send_text", session="default", target="w5:pZ", text=long_text)["success"]
    module.time.sleep(10)
    again = call(module, "control", action="pane_send_text", session="default", target="w5:pZ", text=long_text)
    assert again["error"] == "duplicate_send"


# ---------------------------------------------------------------- fresh_session

def test_fresh_session_verifies_new_session_and_updates_registry(env) -> None:
    module, fake, _, registry = env
    result = call(module, "control", action="fresh_session", session="default", target="w5:pZ", task_id="t1")
    assert result["success"] is True and result["changed"] is True
    assert (result["old_session"], result["new_session"]) == ("sess-1", "sess-2")
    assert [c[6] for c in fake.calls if c[4] == "prompt"] == ["/clear"]
    assert result["registry"]["success"] is True
    task = json.loads(registry.read_text())["tasks"][0]
    assert task["agent_session"] == "sess-2" and task["previous_agent_session"] == "sess-1"
    # the verified idle state lets the brief go out immediately
    assert prompt(module, text="Fresh brief for the new session.")["success"] is True


def test_fresh_session_unchanged_reports_error(env) -> None:
    module, fake, clock, _ = env
    fake.clear_after_polls = None
    start = clock.now
    result = call(module, "control", action="fresh_session", session="default", target="w5:pZ")
    assert result["success"] is False and result["error"] == "session_not_reset"
    assert result["old_session"] == result["new_session"] == "sess-1"
    assert 20 <= clock.now - start <= 22


def test_fresh_session_refuses_working_target_unless_forced(env) -> None:
    module, fake, _, _ = env
    fake.agents["w5:pZ"]["status"] = "working"
    result = call(module, "control", action="fresh_session", session="default", target="w5:pZ")
    assert result["error"] == "target_busy"
    assert not [c for c in fake.calls if c[4] == "prompt"]
    forced = call(module, "control", action="fresh_session", session="default", target="w5:pZ", force=True)
    assert forced["changed"] is True


# ---------------------------------------------------------------- herdr_task

def test_task_tool_roundtrip(env) -> None:
    module, _, _, registry = env
    assert call(module, "task", action="get", task_id="t1")["task"]["pane_id"] == "w5:pZ"
    upd = call(module, "task", action="update", task_id="t1",
               fields={"phase": "review", "wait": {"reason": "user_decision", "note": "Q1"}})
    assert upd["success"] is True and sorted(upd["changed"]) == ["phase", "wait"]
    assert call(module, "task", action="note", task_id="t1", text="checked tests")["success"]
    assert call(module, "task", action="decision", task_id="*", text="deploys allowed", by="user")["success"]
    listed = call(module, "task", action="list")
    assert listed["tasks"][0]["wait"] == "user_decision"
    assert listed["global_decisions"][0]["source"] == "user"
    created = call(module, "task", action="create", task_id="t2", fields={"pane_id": "w5:pQ", "cwd": CWD})
    assert created["task"]["policy"]["allow_push"] is True
    bad = call(module, "task", action="update", task_id="t1", fields={"event9_disposition": "x"})
    assert bad["success"] is False and bad["error"] == "unknown_field"


@pytest.mark.parametrize("args", [
    {"action": "update", "task_id": "t1", "fields": "not-an-object"},
    {"action": "update", "task_id": "t1"},
    {"action": "get"},
    {"action": "get", "task_id": 7},
    {"action": "explode", "task_id": "t1"},
    {"action": "note", "task_id": "t1", "text": None},
    {"action": "get", "task_id": "../../etc/passwd"},
])
def test_task_tool_never_raises(env, args) -> None:
    module, _, _, _ = env
    result = call(module, "task", **args)
    assert result["success"] is False and result["error"] and result["message"]


def test_task_tool_archive_prunes_with_live_panes(env, monkeypatch: pytest.MonkeyPatch) -> None:
    module, fake, _, registry = env
    monkeypatch.setattr(module, "_available", lambda: True)
    call(module, "task", action="create", task_id="gone", fields={"pane_id": "w9:p9", "cwd": CWD})
    result = call(module, "task", action="archive", task_id="gone", text="finished")
    assert result["archived"] is True and result["tombstone_kept"] is False  # w9:p9 not live
    result = call(module, "task", action="archive", task_id="t1")
    assert result["tombstone_kept"] is True  # w5:pZ still live
    assert [t["id"] for t in json.loads(registry.read_text())["tasks"]] == ["t1"]


def test_task_tool_reports_missing_library(env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module, _, _, _ = env
    monkeypatch.setattr(module, "SCRIPTS_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(module, "_REGISTRY_CACHE", None)
    result = call(module, "task", action="list")
    assert result["success"] is False and "registry library missing" in result["message"]
    assert module._registry_available() is False


def test_register_exposes_four_tools() -> None:
    module = load_plugin()

    class Ctx:
        def __init__(self):
            self.tools, self.commands, self.hooks = {}, {}, {}

        def register_tool(self, **kw):
            self.tools[kw["name"]] = kw

        def register_command(self, name, **kw):
            self.commands[name] = kw

        def register_hook(self, name, callback):
            self.hooks[name] = callback

    ctx = Ctx()
    module.register(ctx)
    assert ctx.hooks == {"pre_llm_call": module.open_questions_context}
    assert set(ctx.tools) == {"herdr_inspect", "herdr_control", "herdr_destructive", "herdr_task"}
    assert {t["toolset"] for t in ctx.tools.values()} == {"herdr"}
    assert ctx.tools["herdr_task"]["schema"]["name"] == "herdr_task"
    assert "herdr" in ctx.commands
