import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "herdr_event_bridge.py"
spec = importlib.util.spec_from_file_location("herdr_event_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def test_cwd_prefix_routes_only_matching_project_folders():
    assert bridge.cwd_matches("/srv/example-app", ["/srv/example-app"])
    assert bridge.cwd_matches("/srv/example-app/worktrees/seo", ["/srv/example-app"])
    assert not bridge.cwd_matches("/srv/demo-api", ["/srv/example-app"])
    assert not bridge.cwd_matches("/srv/example-app-other", ["/srv/example-app"])


def test_demoapi_prefix_includes_sibling_worktrees_but_not_similar_projects():
    assert bridge.cwd_matches("/srv/demo-api", ["/srv/demo-api"], sibling_prefix=True)
    assert bridge.cwd_matches("/srv/demo-api-build-optimization", ["/srv/demo-api"], sibling_prefix=True)
    assert not bridge.cwd_matches("/srv/demo-apievil", ["/srv/demo-api"], sibling_prefix=True)
    assert not bridge.cwd_matches("/srv/personal_os", ["/srv/demo-api"], sibling_prefix=True)


def test_merge_discovered_panes_preserves_registered_metadata_and_safely_adds_new_pane():
    registered = {
        "known": {
            "id": "known",
            "session": "default",
            "workspace_id": "w5",
            "pane_id": "w5:p7",
            "agent": "claude",
            "project": "ExampleApp",
            "cwd": "/srv/example-app",
            "change": "known-change",
            "phase": "apply",
            "branch": "feature/x",
            "policy": {"allow_continue": True},
        }
    }
    panes = [
        {"pane_id": "w5:p7", "workspace_id": "w5", "cwd": "/srv/example-app", "agent": "claude"},
        {"pane_id": "w8:p1", "workspace_id": "w8", "cwd": "/srv/example-app", "agent": "codex"},
        {"pane_id": "w7:pA", "workspace_id": "w7", "cwd": "/srv/demo-api", "agent": "claude"},
    ]
    merged = bridge.merge_discovered_panes(
        registered,
        panes,
        project="ExampleApp",
        session="default",
        cwd_prefixes=["/srv/example-app"],
        sibling_prefix=False,
    )
    assert set(merged) == {"known", "auto:w8:p1"}
    assert merged["known"]["change"] == "known-change"
    auto = merged["auto:w8:p1"]
    assert auto["project"] == "ExampleApp"
    assert auto["cwd"] == "/srv/example-app"
    assert auto["policy"] == {
        "allow_continue": False,
        "allow_tests": False,
        "allow_deploy": False,
        "allow_commit": False,
        "allow_push": False,
        "allow_sync_archive": False,
    }


def test_disabled_registry_pane_is_tombstoned_and_not_auto_discovered():
    panes = [
        {"pane_id": "w5:pG", "workspace_id": "w5", "cwd": "/srv/example-app", "agent": "claude"},
        {"pane_id": "w5:pJ", "workspace_id": "w5", "cwd": "/srv/example-app", "agent": "claude"},
    ]
    merged = bridge.merge_discovered_panes(
        {},
        panes,
        project="ExampleApp",
        session="default",
        cwd_prefixes=["/srv/example-app"],
        sibling_prefix=False,
        tombstoned_panes={"w5:pG"},
    )
    assert set(merged) == {"auto:w5:pJ"}


def test_pending_event_is_rejected_after_disable_close_or_phase_change():
    task = {
        "id": "task",
        "pane_id": "w5:p1",
        "phase": "preview",
        "policy": {"allow_continue": False},
    }
    payload = {
        "task": task,
        "task_fingerprint": bridge.task_fingerprint(task),
    }
    assert bridge.pending_is_current(payload, {"task": task}, {"w5:p1"})
    assert not bridge.pending_is_current(payload, {}, {"w5:p1"})
    assert not bridge.pending_is_current(payload, {"task": task}, set())
    newer = {**task, "phase": "apply"}
    assert not bridge.pending_is_current(payload, {"task": newer}, {"w5:p1"})


def test_subscription_watches_global_pane_topology_and_explicit_statuses():
    task = {
        "id": "known",
        "pane_id": "w5:p7",
    }
    request = bridge.subscription_request({"known": task})
    subscriptions = request["params"]["subscriptions"]
    assert {"type": "pane.created"} in subscriptions
    assert {"type": "pane.updated"} in subscriptions
    assert {"type": "pane.closed"} in subscriptions
    assert {"type": "pane.agent_detected"} in subscriptions
    assert {"type": "pane.agent_status_changed", "pane_id": "w5:p7"} in subscriptions


def test_topology_events_trigger_cwd_rediscovery():
    for event in ("pane_created", "pane_updated", "pane_closed", "pane_agent_detected"):
        assert bridge.is_topology_event({"event": event})
    assert not bridge.is_topology_event({"event": "pane.agent_status_changed"})


def test_list_herdr_panes_uses_explicit_session_and_parses_json(tmp_path):
    argv_file = tmp_path / "argv.json"
    fake = tmp_path / "herdr"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'id':'x','result':{'type':'pane_list','panes':[{'pane_id':'w5:p1','cwd':'/srv/example-app'}]}}))\n"
    )
    fake.chmod(0o700)
    panes = bridge.list_herdr_panes(str(fake), "default", timeout=5)
    assert panes == [{"pane_id": "w5:p1", "cwd": "/srv/example-app"}]
    assert json.loads(argv_file.read_text()) == ["--session", "default", "pane", "list"]


def test_topology_rebuild_happens_only_when_routed_pane_set_changes():
    current = {
        "a": {"pane_id": "w5:p1"},
        "b": {"pane_id": "w5:p2"},
    }
    same = {
        "x": {"pane_id": "w5:p2"},
        "y": {"pane_id": "w5:p1"},
    }
    changed = {
        "a": {"pane_id": "w5:p1"},
        "c": {"pane_id": "w5:p3"},
    }
    assert not bridge.topology_requires_rebuild(current, same)
    assert bridge.topology_requires_rebuild(current, changed)


def _registry_task(**overrides):
    task = {
        "id": "task",
        "enabled": True,
        "session": "default",
        "workspace_id": "w5",
        "pane_id": "w5:p1",
        "agent": "claude",
        "project": "ExampleApp",
        "cwd": "/srv/example-app",
        "change": "change",
        "phase": "apply",
        "branch": "main",
        "policy": {
            "allow_continue": True,
            "allow_tests": True,
            "allow_deploy": True,
            "allow_commit": True,
            "allow_push": True,
            "allow_sync_archive": True,
        },
    }
    task.update(overrides)
    return task


def _load_one(tmp_path, task):
    registry = tmp_path / "tasks.json"
    registry.write_text(json.dumps({"version": 1, "tasks": [task]}))
    tasks, _ = bridge.load_tasks(registry)
    return tasks[task["id"]]


def test_load_tasks_infers_micro_for_small_static_change(tmp_path):
    task = _load_one(
        tmp_path,
        _registry_task(changed_files=["html/index.html", "html/css/index.css", "html/js/locales/en.js"]),
    )
    assert task["execution_profile"] == "micro"
    assert task["changed_files"] == ["html/index.html", "html/css/index.css", "html/js/locales/en.js"]


def test_high_risk_flags_override_declared_micro(tmp_path):
    task = _load_one(
        tmp_path,
        _registry_task(
            execution_profile="micro",
            changed_files=["html/index.html"],
            risk_flags={"billing": True},
        ),
    )
    assert task["execution_profile"] == "high-risk"
    assert task["risk_flags"] == {"billing": True}


def test_backend_file_disqualifies_declared_micro(tmp_path):
    task = _load_one(
        tmp_path,
        _registry_task(execution_profile="micro", changed_files=["telegram_audience_quality/bot.py"]),
    )
    assert task["execution_profile"] == "standard"


def test_read_only_task_defaults_to_research(tmp_path):
    policy = {key: False for key in _registry_task()["policy"]}
    task = _load_one(tmp_path, _registry_task(phase="read-only Explore", policy=policy))
    assert task["execution_profile"] == "research"


def test_evidence_status_is_current_only_for_matching_revision(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = repo / "evidence.json"
    manifest.write_text(json.dumps({"candidate_sha": "a" * 40, "deploy_revision": "site-7"}))
    task = {
        "cwd": str(repo),
        "evidence_manifest": str(manifest),
        "candidate_sha": "a" * 40,
        "deploy_revision": "site-7",
    }
    current = bridge.evidence_status(task)
    assert current["status"] == "current"
    assert current["manifest_sha256"]
    task["candidate_sha"] = "b" * 40
    stale = bridge.evidence_status(task)
    assert stale == {"status": "stale", "reasons": ["candidate_sha_mismatch"]}


def test_evidence_status_rejects_symlink_escape_from_task_cwd(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"candidate_sha": "a" * 40}))
    link = repo / "evidence.json"
    link.symlink_to(outside)
    task = {
        "cwd": str(repo),
        "evidence_manifest": str(link),
        "candidate_sha": "a" * 40,
        "deploy_revision": "",
    }
    assert bridge.evidence_status(task) == {"status": "invalid", "reasons": ["manifest_outside_cwd"]}


def test_transition_payload_carries_profile_evidence_and_phase_telemetry(tmp_path):
    manifest = tmp_path / "evidence.json"
    manifest.write_text(json.dumps({"candidate_sha": "c" * 40}))
    task = {
        "id": "task",
        "pane_id": "w5:p1",
        "phase": "apply",
        "execution_profile": "micro",
        "cwd": str(tmp_path),
        "evidence_manifest": str(manifest),
        "candidate_sha": "c" * 40,
        "deploy_revision": "",
    }
    state = {"version": 1, "panes": {}, "pending": []}
    bridge.transition(state, task, "working", now="2026-09-04T10:00:00+00:00")
    payload = bridge.transition(state, task, "done", now="2026-09-04T10:00:05+00:00")
    assert payload["task"]["execution_profile"] == "micro"
    assert payload["evidence"]["status"] == "current"
    assert payload["telemetry"]["last_status_duration_seconds"] == 5.0
    assert payload["telemetry"]["phase"] == "apply"


def test_profile_change_invalidates_queued_event():
    task = {"id": "task", "pane_id": "w5:p1", "execution_profile": "micro"}
    payload = {"task": task, "task_fingerprint": bridge.task_fingerprint(task)}
    newer = {**task, "execution_profile": "standard"}
    assert not bridge.pending_is_current(payload, {"task": newer}, {"w5:p1"})
