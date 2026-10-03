import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("registry_wait_recheck_test", SCRIPTS / "herdr_registry.py")
reg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reg)


def registry_with(tmp_path, **extra):
    (tmp_path / "state").mkdir()
    registry = tmp_path / "state" / "herdr_tasks.json"
    registry.write_text(json.dumps({"version": 1, "tasks": []}))
    reg.create({"id": "t1", "pane_id": "w5:p1", "cwd": str(tmp_path), **extra}, "test", registry)
    return registry


def wait_of(registry):
    return json.loads(registry.read_text())["tasks"][0]["wait"]


def hours_ahead(until):
    at = datetime.fromisoformat(until.replace("Z", "+00:00"))
    return (at - datetime.now(timezone.utc)) / timedelta(hours=1)


def test_owner_question_without_until_is_rechecked_in_three_hours(tmp_path):
    # An unanswered owner question once held a task for 88 h: without `until` the
    # watchdog never woke the controller again.
    registry = registry_with(tmp_path)
    reg.update("t1", {"wait": {"reason": "user_decision", "note": "6 ч или ждать?"}}, registry=registry)
    assert 2.9 < hours_ahead(wait_of(registry)["until"]) <= 3.0


def test_author_external_wait_without_until_is_rechecked_in_an_hour(tmp_path):
    registry = registry_with(tmp_path)
    reg.update("t1", {"wait": {"reason": "external", "note": "ждёт ветку соседа"}}, registry=registry)
    assert 0.9 < hours_ahead(wait_of(registry)["until"]) <= 1.0


def test_reviewer_external_wait_keeps_no_deadline(tmp_path):
    # A reviewer waits on its author, whose events drive the workflow.
    registry = registry_with(tmp_path, parent_task_id="t0")
    reg.update("t1", {"wait": {"reason": "external", "note": "ревью пройдено"}}, registry=registry)
    assert wait_of(registry)["until"] == ""


def test_explicit_until_is_kept(tmp_path):
    registry = registry_with(tmp_path)
    reg.update("t1", {"wait": {"reason": "user_decision", "until": "2030-01-01T09:00:00Z"}}, registry=registry)
    assert wait_of(registry)["until"] == "2030-01-01T09:00:00Z"


def test_setting_owner_question_reminds_to_send_it(tmp_path):
    registry = registry_with(tmp_path)
    result = reg.update("t1", {"wait": {"reason": "user_decision", "note": "Разрешаешь?"}}, registry=registry)
    assert "[SILENT]" in result["reminder"]
    again = reg.update("t1", {"phase": "x"}, registry=registry)
    assert "reminder" not in again
