"""Tests for scripts/herdr_registry.py (tmp registries only; no live state)."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
STAGE_REGISTRY = SCRIPTS.parent / "herdr_tasks.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


reg = _load("herdr_registry_under_test", SCRIPTS / "herdr_registry.py")
bridge = _load("herdr_event_bridge_for_registry_test", SCRIPTS / "herdr_event_bridge.py")

CWD = "/srv/example-app"


def base_task(task_id: str = "t1", pane: str = "w5:p1", **extra) -> dict:
    task = {
        "id": task_id, "enabled": True, "session": "default", "workspace_id": "w5",
        "pane_id": pane, "agent": "claude", "project": "ExampleApp", "cwd": CWD,
        "change": "c", "phase": "impl", "branch": "main",
        "policy": {k: True for k in reg.POLICY_KEYS},
    }
    task.update(extra)
    return task


def write_registry(path: Path, tasks: list[dict], **top) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "tasks": tasks, **top}, indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def registry(tmp_path: Path) -> Path:
    return write_registry(tmp_path / "state" / "herdr_tasks.json", [base_task()])


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def task_of(path: Path, task_id: str) -> dict:
    return next(t for t in read(path)["tasks"] if t["id"] == task_id)


# ---------------------------------------------------------------- create

def test_create_defaults_full_autonomy_policy_and_standard_profile(registry: Path) -> None:
    result = reg.create({"id": "t2", "pane_id": "w7:p3", "cwd": CWD}, registry=registry)
    assert result["success"] is True
    task = task_of(registry, "t2")
    assert task["policy"] == {k: True for k in reg.POLICY_KEYS}
    assert task["execution_profile"] == "standard"
    assert task["workspace_id"] == "w7"
    assert task["project"] == "ExampleApp"  # inferred from existing task with same cwd
    assert task["wait"] == {"reason": "none", "until": "", "note": ""}
    assert task["updated_at"]
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    assert (registry.parent / "herdr_tasks.lock").exists()
    loaded, _ = bridge.load_tasks(registry)
    assert set(loaded) == {"t1", "t2"}


def test_create_partial_policy_overrides_only_given_keys(registry: Path) -> None:
    reg.create({"id": "t2", "pane_id": "w7:p3", "cwd": CWD, "policy": {"allow_deploy": False}}, registry=registry)
    policy = task_of(registry, "t2")["policy"]
    assert policy["allow_deploy"] is False
    assert all(policy[k] for k in reg.POLICY_KEYS if k != "allow_deploy")


@pytest.mark.parametrize("task,code", [
    ({"id": "t1", "pane_id": "w7:p3", "cwd": CWD}, "task_exists"),
    ({"id": "t2", "pane_id": "w5:p1", "cwd": CWD}, "pane_in_use"),
    ({"id": "t2", "pane_id": "w7:p3", "cwd": "relative/dir"}, "invalid_field"),
    ({"id": "t2", "pane_id": "w7:p3"}, "invalid_field"),
    ({"id": "bad id", "pane_id": "w7:p3", "cwd": CWD}, "invalid_field"),
    ({"id": "t2", "pane_id": "w7:p3", "cwd": CWD, "event7_disposition": "x"}, "unknown_field"),
])
def test_create_rejections(registry: Path, task: dict, code: str) -> None:
    before = registry.read_bytes()
    with pytest.raises(reg.RegistryError) as info:
        reg.create(task, registry=registry)
    assert info.value.code == code
    assert registry.read_bytes() == before


# ---------------------------------------------------------------- update

def test_update_whitelisted_fields_and_updated_at(registry: Path) -> None:
    result = reg.update("t1", {"phase": "review", "phase_generation": 3, "candidate_sha": "ABCDEF1"}, registry=registry)
    assert result["success"] is True and sorted(result["changed"]) == ["candidate_sha", "phase", "phase_generation"]
    task = task_of(registry, "t1")
    assert task["phase"] == "review" and task["phase_generation"] == 3
    assert task["candidate_sha"] == "abcdef1"
    assert task["updated_at"]


def test_update_rejects_unknown_and_managed_keys_with_clear_error(registry: Path) -> None:
    with pytest.raises(reg.RegistryError) as info:
        reg.update("t1", {"event7_disposition": "x"}, registry=registry)
    assert info.value.code == "unknown_field"
    assert "allowed:" in info.value.message and "phase" in info.value.message
    for managed in ("id", "notes", "decisions", "legacy_path", "updated_at"):
        with pytest.raises(reg.RegistryError) as info:
            reg.update("t1", {managed: "x"}, registry=registry)
        assert info.value.code == "managed_field"


@pytest.mark.parametrize("fields", [
    {"enabled": "yes"},
    {"phase_generation": -1},
    {"phase_generation": True},
    {"execution_profile": "yolo"},
    {"changed_files": ["../etc/passwd"]},
    {"changed_files": "/abs"},
    {"risk_flags": {"Bad-Key": True}},
    {"policy": {"allow_everything": True}},
    {"policy": {"allow_deploy": "yes"}},
    {"candidate_sha": "not-a-sha"},
    {"brief": "x" * 2001},
    {"phase": "x" * 301},
    {"cwd": "relative"},
    {"session": "a b"},
    {"wait": {"reason": "sleeping"}},
    {"wait": {"reason": "scheduled"}},
    {"wait": {"reason": "external", "until": "2026-09-24T10:00:00"}},
    {"wait": {"reason": "external", "until": "2026-09-24T10:00:00+03:00"}},
    {"wait": {"reason": "none", "note": "x" * 301}},
    {"wait": {"reason": "external", "quiet": True}},
    {"wait": {"reason": "scheduled", "until": "2026-09-24T10:00:00Z", "quiet": "yes"}},
    {"handoff": {"blob": "x" * 2100}},
    {"pane_id": None},
])
def test_update_type_checks(registry: Path, fields: dict) -> None:
    before = registry.read_bytes()
    with pytest.raises(reg.RegistryError):
        reg.update("t1", fields, registry=registry)
    assert registry.read_bytes() == before
    assert not list(registry.parent.glob(".herdr_tasks.json.tmp-*"))


def test_policy_partial_merge_never_escalates_missing_keys(tmp_path: Path) -> None:
    # A legacy record whose policy omits deploy/commit/push/sync: the bridge
    # treats those as False; a partial update must keep them False.
    path = write_registry(tmp_path / "herdr_tasks.json", [base_task(policy={"allow_continue": True})])
    reg.update("t1", {"policy": {"allow_tests": False}}, registry=path)
    policy = task_of(path, "t1")["policy"]
    assert policy == {"allow_continue": True, "allow_tests": False, "allow_deploy": False,
                      "allow_commit": False, "allow_push": False, "allow_sync_archive": False}


def test_generation_increment_and_compare_and_set(registry: Path) -> None:
    reg.update("t1", {"phase_generation": 4}, registry=registry)
    result = reg.update("t1", {"phase_generation": "+1", "phase": "p5"}, expect_generation=4, registry=registry)
    assert result["phase_generation"] == 5
    with pytest.raises(reg.RegistryError) as info:
        reg.update("t1", {"phase": "stale"}, expect_generation=4, registry=registry)
    assert info.value.code == "generation_conflict"
    assert task_of(registry, "t1")["phase"] == "p5"


def test_null_removes_optional_key_and_compat_keys_are_accepted(registry: Path) -> None:
    reg.update("t1", {"handoff": {"id": "h1", "status": "sent"}, "controller_disposition": "waiting",
                      "brief": "b"}, registry=registry)
    task = task_of(registry, "t1")
    assert task["handoff"] == {"id": "h1", "status": "sent"}
    reg.update("t1", {"handoff": None, "brief": None}, registry=registry)
    task = task_of(registry, "t1")
    assert "handoff" not in task and "brief" not in task
    assert task["controller_disposition"] == "waiting"


def test_wait_is_normalized_to_utc_z(registry: Path) -> None:
    reg.update("t1", {"wait": {"reason": "scheduled", "until": "2026-09-24T10:00:00+00:00", "note": "cron"}},
               registry=registry)
    assert task_of(registry, "t1")["wait"] == {"reason": "scheduled", "until": "2026-09-24T10:00:00Z", "note": "cron"}


def test_quiet_wait_is_kept_only_when_true(registry: Path) -> None:
    until = "2026-09-24T10:00:00Z"
    reg.update("t1", {"wait": {"reason": "scheduled", "until": until, "quiet": True}}, registry=registry)
    assert task_of(registry, "t1")["wait"] == {"reason": "scheduled", "until": until, "note": "", "quiet": True}
    reg.update("t1", {"wait": {"reason": "scheduled", "until": until, "quiet": False}}, registry=registry)
    assert task_of(registry, "t1")["wait"] == {"reason": "scheduled", "until": until, "note": ""}


def test_noop_update_does_not_rewrite_registry(registry: Path) -> None:
    reg.update("t1", {"phase": "same"}, registry=registry)
    before = registry.stat().st_mtime_ns, registry.read_bytes()
    result = reg.update("t1", {"phase": "same"}, registry=registry)
    assert result["changed"] == []
    assert (registry.stat().st_mtime_ns, registry.read_bytes()) == before


def test_update_missing_task_and_archived_stub(registry: Path) -> None:
    with pytest.raises(reg.RegistryError) as info:
        reg.update("nope", {"phase": "x"}, registry=registry)
    assert info.value.code == "task_not_found"
    reg.archive("t1", registry=registry)
    with pytest.raises(reg.RegistryError) as info:
        reg.update("t1", {"enabled": True}, registry=registry)
    assert info.value.code == "task_archived"


def test_evidence_manifest_outside_cwd_is_rejected_before_install(registry: Path) -> None:
    before = registry.read_bytes()
    with pytest.raises(reg.RegistryError) as info:
        reg.update("t1", {"evidence_manifest": "/etc/passwd"}, registry=registry)
    assert info.value.code in {"invalid_registry", "bridge_rejected"}
    assert registry.read_bytes() == before


def test_bridge_loader_is_consulted_on_the_exact_bytes(registry: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def picky_loader(path: Path):
        seen.append(Path(path).read_text())
        raise ValueError("future bridge rule")

    monkeypatch.setattr(reg, "_BRIDGE_LOADER", picky_loader)
    before = registry.read_bytes()
    with pytest.raises(reg.RegistryError) as info:
        reg.update("t1", {"phase": "x"}, registry=registry)
    assert info.value.code == "bridge_rejected"
    assert '"phase": "x"' in seen[0]
    assert registry.read_bytes() == before


def test_real_bridge_loader_is_found_next_to_the_library() -> None:
    loader = reg._bridge_loader()
    assert callable(loader) and loader.__name__ == "load_tasks"


def test_incompatible_bridge_loader_does_not_block_writes(registry: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg, "_BRIDGE_LOADER", lambda path, extra: None)  # TypeError on call
    assert reg.update("t1", {"phase": "x"}, registry=registry)["success"] is True


# ---------------------------------------------------------------- notes / decisions / view

def test_notes_and_decisions_are_capped_and_view_is_compact(registry: Path) -> None:
    for i in range(25):
        reg.add_note("t1", f"note {i}", "controller", registry)
        reg.add_decision("t1", f"decision {i}", "user", registry)
    task = task_of(registry, "t1")
    assert len(task["notes"]) == 20 and task["notes"][0]["text"] == "note 5"
    assert len(task["decisions"]) == 20 and task["decisions"][-1] == {
        "at": task["decisions"][-1]["at"], "source": "user", "text": "decision 24"}
    view = reg.get("t1", registry)["task"]
    assert [n["text"] for n in view["notes"]] == [f"note {i}" for i in range(20, 25)]
    assert view["notes_total"] == 20 and view["decisions_total"] == 20
    assert view["wait"]["reason"] == "none"


def test_note_text_bounds(registry: Path) -> None:
    with pytest.raises(reg.RegistryError):
        reg.add_note("t1", "x" * 601, "c", registry)
    with pytest.raises(reg.RegistryError):
        reg.add_note("t1", "   ", "c", registry)


def test_global_decisions_capped_at_50(registry: Path) -> None:
    for i in range(55):
        reg.add_decision("*", f"g{i}", "user", registry)
    data = read(registry)
    assert len(data["decisions"]) == 50 and data["decisions"][0]["text"] == "g5"
    assert reg.get("*", registry)["decisions_total"] == 50
    bridge.load_tasks(registry)  # top-level decisions do not disturb the bridge


def test_view_lists_unmigrated_keys_by_name_only(tmp_path: Path) -> None:
    path = write_registry(tmp_path / "herdr_tasks.json", [base_task(event7_disposition="long prose")])
    view = reg.get("t1", path)["task"]
    assert view["unmigrated_keys"] == ["event7_disposition"]
    assert "event7_disposition" not in view


def test_list_hides_disabled_and_stubs_by_default(registry: Path) -> None:
    reg.create({"id": "t2", "pane_id": "w5:p2", "cwd": CWD}, registry=registry)
    reg.archive("t2", registry=registry)
    assert [t["id"] for t in reg.list_tasks(True, registry)["tasks"]] == ["t1"]
    rows = reg.list_tasks(False, registry)["tasks"]
    assert [r.get("tombstone", False) for r in rows] == [False, True]


# ---------------------------------------------------------------- locking / atomicity

def test_lock_busy_is_reported(registry: Path) -> None:
    fd = os.open(registry.with_name("herdr_tasks.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(reg.RegistryError) as info:
            reg.transaction(registry, lambda data: {}, lock_timeout=0.2)
        assert info.value.code == "lock_busy"
        with pytest.raises(reg.RegistryError) as info:
            reg.migrate(dry_run=False, registry=registry, lock_timeout=0.1)
        assert info.value.code == "lock_busy"
        assert reg.migrate(dry_run=True, registry=registry)["success"] is True  # read-only
    finally:
        os.close(fd)


def test_concurrent_writers_lose_nothing(registry: Path) -> None:
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(5):
                reg.add_decision("*", f"w{n}-{i}", "controller", registry)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(read(registry)["decisions"]) == 40


def test_unlocked_manual_edit_during_update_is_not_lost(registry: Path) -> None:
    calls = {"n": 0}

    def mutate(data: dict) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:  # simulate a hand edit racing the locked writer
            raw = read(registry)
            raw["tasks"][0]["branch"] = "hand-edited"
            registry.write_text(json.dumps(raw), encoding="utf-8")
        data["tasks"][0]["phase"] = "locked-write"
        return {"success": True}

    reg.transaction(registry, mutate)
    task = task_of(registry, "t1")
    assert calls["n"] == 2
    assert task["branch"] == "hand-edited" and task["phase"] == "locked-write"


def test_registry_size_and_task_count_bounds(tmp_path: Path) -> None:
    path = write_registry(tmp_path / "herdr_tasks.json",
                          [base_task(f"t{i}", f"w1:p{i}", enabled=False) for i in range(100)])
    with pytest.raises(reg.RegistryError) as info:
        reg.create({"id": "t100", "pane_id": "w2:p1", "cwd": CWD}, registry=path)
    assert info.value.code == "registry_full"


def test_legacy_file_names_are_confined(tmp_path: Path) -> None:
    registry = tmp_path / "herdr_tasks.json"
    names = set()
    for task_id in ("auto:w5:p1", "a/../../etc", "auto_w5_p1", "plain-id"):
        path = reg.legacy_file(registry, task_id)
        assert path.parent == tmp_path / "herdr_tasks_legacy"
        assert path.resolve().parent == (tmp_path / "herdr_tasks_legacy").resolve()
        assert not path.name.startswith(".")
        names.add(path.name)
    assert len(names) == 4  # sanitized ids get a hash suffix, so no collisions
    assert reg.legacy_file(registry, "plain-id").name == "plain-id.json"


# ---------------------------------------------------------------- archive

def test_archive_appends_full_record_and_leaves_tombstone(registry: Path) -> None:
    reg.add_note("t1", "context", "c", registry)
    result = reg.archive("t1", registry=registry)
    assert result["archived"] is True and result["tombstone_kept"] is True
    lines = (registry.parent / "herdr_tasks_archive.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["task"]["notes"][0]["text"] == "context"
    stub = task_of(registry, "t1")
    assert stub["enabled"] is False and stub["tombstone"] is True and stub["archived_at"]
    assert "notes" not in stub and "policy" not in stub
    assert bridge.load_tombstoned_panes(registry) == {"w5:p1"}
    assert bridge.load_tasks(registry)[0] == {}
    assert reg.archive("t1", registry=registry)["already_archived"] is True
    assert len((registry.parent / "herdr_tasks_archive.jsonl").read_text().splitlines()) == 1


def test_archive_with_live_panes_prunes_closed_stubs(registry: Path) -> None:
    reg.create({"id": "t2", "pane_id": "w5:p2", "cwd": CWD}, registry=registry)
    reg.archive("t2", registry=registry)  # no live info: stub kept
    result = reg.archive("t1", live_panes={"w5:p1"}, registry=registry)
    assert result["pruned_tombstones"] == ["t2"] and result["tombstone_kept"] is True
    assert [t["id"] for t in read(registry)["tasks"]] == ["t1"]


# ---------------------------------------------------------------- migrate

def legacy_registry(tmp_path: Path) -> Path:
    tasks = [
        base_task("active", "w5:pX", brief="b" * 2500, phase="p" * 350, event7_disposition="old prose",
                  handoff={"id": "h", "status": "execution_verified"}, latest_user_decision="x" * 2100,
                  phase_generation=7, agent_session="s-1"),
        base_task("off", "w5:p7", enabled=False, observation={"k": "v"}),
        base_task("done", "w5:pM", completed=True),
    ]
    return write_registry(tmp_path / "state" / "herdr_tasks.json", tasks)


def test_migrate_dry_run_reports_and_writes_nothing(tmp_path: Path) -> None:
    path = legacy_registry(tmp_path)
    before = path.read_bytes()
    summary = reg.migrate(dry_run=True, registry=path)
    assert summary["changed"] is True and summary["dry_run"] is True
    assert summary["bytes_after"] < summary["bytes_before"]
    actions = {t["id"]: t for t in summary["tasks"]}
    assert actions["active"]["moved_keys"] == ["brief_full", "event7_disposition", "latest_user_decision", "phase_full"]
    assert actions["off"]["action"] == actions["done"]["action"] == "archived"
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["herdr_tasks.json"]


def test_migrate_apply_is_lossless_idempotent_and_bridge_neutral(tmp_path: Path) -> None:
    path = legacy_registry(tmp_path)
    original = read(path)
    before_tasks, _ = bridge.load_tasks(path)
    summary = reg.migrate(dry_run=False, registry=path)
    assert summary["archived"] == 2 and summary["slimmed"] == 1
    active = task_of(path, "active")
    assert len(active["brief"]) == 2000 and len(active["phase"]) == 300
    assert active["handoff"] == {"id": "h", "status": "execution_verified"}  # compat key kept
    assert "event7_disposition" not in active and "latest_user_decision" not in active
    legacy = json.loads(Path(active["legacy_path"]).read_text())
    assert legacy["fields"]["brief_full"] == "b" * 2500
    assert legacy["fields"]["event7_disposition"] == "old prose"
    assert stat.S_IMODE(Path(active["legacy_path"]).stat().st_mode) == 0o600
    archived = [json.loads(l)["task"] for l in (path.parent / "herdr_tasks_archive.jsonl").read_text().splitlines()]
    assert archived == [original["tasks"][1], original["tasks"][2]]
    assert bridge.load_tombstoned_panes(path) == {"w5:p7", "w5:pM"}
    after_tasks, _ = bridge.load_tasks(path)
    assert {k: bridge.task_fingerprint(v) for k, v in after_tasks.items()} == {
        k: bridge.task_fingerprint(v) for k, v in before_tasks.items() if k == "active"}
    snapshot = path.read_bytes(), path.stat().st_mtime_ns
    again = reg.migrate(dry_run=False, registry=path)
    assert again["changed"] is False and again["archived"] == 0
    assert (path.read_bytes(), path.stat().st_mtime_ns) == snapshot
    assert len((path.parent / "herdr_tasks_archive.jsonl").read_text().splitlines()) == 2


def test_migrate_merges_existing_legacy_sidecar_with_history(tmp_path: Path) -> None:
    path = legacy_registry(tmp_path)
    reg.migrate(dry_run=False, registry=path)
    raw = read(path)
    raw["tasks"][0]["event7_disposition"] = "newer prose"  # a controller hand-added it again
    path.write_text(json.dumps(raw), encoding="utf-8")
    reg.migrate(dry_run=False, registry=path)
    legacy = json.loads(Path(task_of(path, "active")["legacy_path"]).read_text())
    assert legacy["fields"]["event7_disposition"] == "newer prose"
    assert legacy["history"][0]["key"] == "event7_disposition" and legacy["history"][0]["value"] == "old prose"


def test_migrate_live_panes_drop_tombstones_of_closed_panes(tmp_path: Path) -> None:
    path = legacy_registry(tmp_path)
    summary = reg.migrate(dry_run=False, live_panes={"w5:pX", "w5:p7"}, registry=path)
    assert summary["tombstones"] == 1
    assert [t["id"] for t in read(path)["tasks"]] == ["active", "off"]


def test_migrate_real_registry_copy(tmp_path: Path) -> None:
    if not STAGE_REGISTRY.exists():
        pytest.skip("sample registry not present")
    path = tmp_path / "state" / "herdr_tasks.json"
    path.parent.mkdir()
    shutil.copy(STAGE_REGISTRY, path)
    before, _ = bridge.load_tasks(path)
    tombstones = bridge.load_tombstoned_panes(path)
    dry = reg.migrate(dry_run=True, registry=path)
    assert dry["bytes_after"] < dry["bytes_before"] / 2
    reg.migrate(dry_run=False, registry=path)
    after, _ = bridge.load_tasks(path)
    assert {k: bridge.task_fingerprint(v) for k, v in after.items()} == {
        k: bridge.task_fingerprint(v) for k, v in before.items()}
    assert bridge.load_tombstoned_panes(path) == tombstones
    for task in read(path)["tasks"]:
        if task.get("enabled", True):
            assert set(task) <= set(reg.CORE_FIELDS) | set(reg.COMPAT_FIELDS)
    assert reg.migrate(dry_run=False, registry=path)["changed"] is False


# ---------------------------------------------------------------- CLI

def run_cli(*args: str, env: dict | None = None) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(SCRIPTS / "herdr_registry.py"), *args],
                          capture_output=True, text=True, timeout=60, env=env)
    return proc.returncode, json.loads(proc.stdout)


def test_cli_roundtrip(registry: Path) -> None:
    r = str(registry)
    assert run_cli("--registry", r, "note", "t1", "hello")[0] == 0
    assert run_cli("--registry", r, "decision", "*", "global rule")[0] == 0
    code, out = run_cli("--registry", r, "update", "t1", json.dumps({"phase": "cli"}))
    assert code == 0 and out["changed"] == ["phase"]
    code, out = run_cli("--registry", r, "update", "t1", json.dumps({"bogus": 1}))
    assert code == 1 and out["error"] == "unknown_field"
    code, out = run_cli("--registry", r, "show", "t1")
    assert out["task"]["phase"] == "cli" and out["task"]["notes"][0]["text"] == "hello"
    code, out = run_cli("--registry", r, "list")
    assert out["count"] == 1 and out["global_decisions"][0]["text"] == "global rule"
    code, out = run_cli("--registry", r, "migrate")
    assert code == 0 and out["dry_run"] is True


def test_cli_default_path_from_hermes_home(registry: Path) -> None:
    env = {**os.environ, "HERMES_HOME": str(registry.parent.parent)}
    code, out = run_cli("show", "t1", env=env)
    assert code == 0 and out["task"]["id"] == "t1"
