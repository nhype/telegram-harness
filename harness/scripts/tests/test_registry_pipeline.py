import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("registry_pipeline_test", SCRIPTS / "herdr_registry.py")
reg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reg)


def home(tmp_path, project=None):
    (tmp_path / "state").mkdir()
    registry = tmp_path / "state" / "herdr_tasks.json"
    registry.write_text(json.dumps({"version": 1, "tasks": []}))
    if project:
        (tmp_path / "herdr-pipeline.json").write_text(json.dumps({
            "profile": "demo", "project": project, "cwd_prefixes": ["/srv/demo-api"],
            "sibling_prefix": True, "route": "herdr-agent-events"}))
    cwd = tmp_path / "demoapi-worktrees" / "pricing-cost-audit"
    cwd.mkdir(parents=True)
    return registry, str(cwd)


def created_project(registry):
    return json.loads(registry.read_text())["tasks"][0]["project"]


def test_new_worktree_task_gets_profile_project(tmp_path):
    registry, cwd = home(tmp_path, "DemoApi")
    reg.create({"id": "mp-x", "pane_id": "w7:p9", "cwd": cwd}, "test", registry)
    assert created_project(registry) == "DemoApi"


def test_without_pipeline_config_project_is_cwd_name(tmp_path):
    registry, cwd = home(tmp_path)
    reg.create({"id": "mp-x", "pane_id": "w7:p9", "cwd": cwd}, "test", registry)
    assert created_project(registry) == "pricing-cost-audit"


def test_default_registry_requires_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with pytest.raises(reg.RegistryError):
        reg.default_registry_path()


def test_default_registry_uses_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert reg.default_registry_path() == tmp_path / "state" / "herdr_tasks.json"


def test_caller_project_is_replaced_by_profile_project(tmp_path):
    # A chat session once created a task with project='demoapi'; the route script
    # matches the profile project exactly, so every event of that task was dropped.
    registry, cwd = home(tmp_path, "DemoApi")
    reg.create({"id": "mp-x", "pane_id": "w7:p9", "cwd": cwd, "project": "demoapi"}, "test", registry)
    assert created_project(registry) == "DemoApi"
