"""The owner's chat turn sees which questions agents are waiting on."""
import importlib.util
import json
from pathlib import Path

PLUGIN = Path(__file__).parents[1] / "__init__.py"
OWNER = "111111111"


def load(home, monkeypatch, name="oq"):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profile(tmp_path, tasks, owner=OWNER):
    home = tmp_path / "exampleapp"
    (home / "state").mkdir(parents=True)
    (home / "herdr-pipeline.json").write_text(json.dumps({
        "profile": "exampleapp", "project": "ExampleApp", "cwd_prefixes": ["/srv/example-app"],
        "route": "herdr-agent-events", "chat_id": owner, "owner_user_id": owner,
        "approval_tag": "[ExampleApp Herdr approval]"}))
    (home / "state" / "herdr_tasks.json").write_text(json.dumps({"version": 1, "tasks": tasks}))
    return home


def task(task_id, reason, note="", **extra):
    return {"id": task_id, "enabled": True, "completed": False, "session": "default",
            "pane_id": "w5:p11", "cwd": "/srv/example-app", "policy": {},
            "updated_at": "2026-09-29T22:36:46+00:00",
            "wait": {"reason": reason, "until": "", "note": note}, **extra}


def test_owner_message_gets_pending_question(tmp_path, monkeypatch):
    # The owner once answered a bare «yes» to a controller question; the chat
    # session had never seen that question and the task sat 11 h.
    home = profile(tmp_path, [task("export", "user_decision", "Включить экспорт в CSV для всех пользователей?"),
                              task("enrichment", "scheduled")])
    m = load(home, monkeypatch)
    out = m.open_questions_context(platform="telegram", sender_id=OWNER)
    assert "export" in out["context"] and "Включить экспорт в CSV для всех пользователей?" in out["context"]
    assert "enrichment" not in out["context"]


def test_nothing_injected_without_pending_questions(tmp_path, monkeypatch):
    m = load(profile(tmp_path, [task("enrichment", "scheduled"), task("done", "user_decision", completed=True)]),
             monkeypatch)
    assert m.open_questions_context(platform="telegram", sender_id=OWNER) is None


def test_controller_and_strangers_get_nothing(tmp_path, monkeypatch):
    m = load(profile(tmp_path, [task("export", "user_decision", "?")]), monkeypatch)
    assert m.open_questions_context(platform="webhook", sender_id="") is None
    assert m.open_questions_context(platform="telegram", sender_id="999") is None


def test_neighbour_profile_scope_stays_silent(tmp_path, monkeypatch):
    # Each gateway also imports the neighbour profile's plugins; only the live home injects.
    home = profile(tmp_path, [task("export", "user_decision", "?")])
    m = load(home, monkeypatch)
    monkeypatch.setattr(m, "get_hermes_home", lambda: tmp_path / "demo")
    assert m.open_questions_context(platform="telegram", sender_id=OWNER) is None


def test_unconfigured_profile_injects_nothing(tmp_path, monkeypatch):
    home = profile(tmp_path, [task("export", "user_decision", "?")])
    (home / "herdr-pipeline.json").unlink()
    m = load(home, monkeypatch)
    assert m.open_questions_context(platform="telegram", sender_id=OWNER) is None
