#!/usr/bin/env python3
"""Locked, validated access to the Herdr task registry (state/herdr_tasks.json).

Dependency-free library + CLI used by the herdr-control plugin (tool
``herdr_task``) and by operators.  Every write happens under an fcntl lock
(``herdr_tasks.lock`` next to the registry), is validated with the same rules
as ``herdr_event_bridge.load_tasks`` (and, when importable, by that loader
itself against the exact bytes about to be installed), and is installed
atomically (tmp file + ``os.replace``, mode 0600).

Archived/disabled tasks keep a small tombstone stub in ``tasks[]`` because the
event bridge treats every ``enabled: false`` record as "do not resurrect this
still-live pane as auto:<pane>".  The full record goes to
``herdr_tasks_archive.jsonl``.  Stubs can be pruned once their pane is gone.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
RISK_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_TASKS = 100
MAX_REGISTRY_BYTES = 256 * 1024
MAX_READ_BYTES = 4 * 1024 * 1024
MAX_NOTES = 20
MAX_TASK_DECISIONS = 20
MAX_GLOBAL_DECISIONS = 50
MAX_ENTRY_TEXT = 600
MAX_BRIEF = 2000
MAX_PHASE = 300
MAX_COMPAT_JSON = 2000
VIEW_TAIL = 5
LOCK_TIMEOUT_S = 5.0
EXECUTION_PROFILES = ("micro", "standard", "high-risk", "research")
WAIT_REASONS = ("none", "user_decision", "external", "scheduled", "complete")
# A wait on a person or on another task still gets re-checked: without `until` the
# watchdog never wakes the controller again (an unanswered owner question held a task 88 h).
RECHECK_SECONDS = {"user_decision": 3 * 3600, "external": 3600}
OWNER_QUESTION_REMINDER = ("The owner has not been asked yet: your final answer must be this question "
                           "to the owner, not [SILENT].")
POLICY_KEYS = (
    "allow_continue", "allow_tests", "allow_deploy",
    "allow_commit", "allow_push", "allow_sync_archive",
)
# herdr_event_bridge.load_tasks defaults for a key missing from policy{}.
BRIDGE_POLICY_DEFAULTS = {
    "allow_continue": True, "allow_tests": True, "allow_deploy": False,
    "allow_commit": False, "allow_push": False, "allow_sync_archive": False,
}

# Canonical order of core task keys (kept by migrate, shown by get()).
CORE_FIELDS = (
    "id", "enabled", "completed", "session", "workspace_id", "tab_id", "pane_id",
    "agent", "agent_session", "previous_agent_session", "project", "cwd", "change",
    "phase", "phase_generation", "branch", "execution_profile", "changed_files",
    "risk_flags", "policy", "candidate_sha", "deploy_revision", "evidence_manifest",
    "parent_task_id", "parent_pane_id", "reviewer_pane_id", "reviewer_task_id", "brief", "brief_path",
    "kanban_task_id", "legacy_path", "wait", "notes", "decisions", "tombstone",
    "archived_at", "updated_at",
)
# Keys projected into webhook payloads by herdr_workflow_context.FIELDS (and so
# read by controllers / herdr-controller-guard).  migrate keeps them in place
# (bounded); update accepts them so controllers never hand-edit the JSON.
COMPAT_FIELDS = (
    "review_verdict", "final_artifact", "review_gate", "controller_gate",
    "controller_disposition", "controller_findings_current", "latest_user_decision",
    "decision_ledger", "deployment_scope", "budget_gate_reconciliation",
    "fresh_session_verified", "expected_review_artifact", "previous_review_artifact",
    "handoff", "current_review_handoff", "last_controller_event",
    "last_controller_checkpoint", "flow_checkpoint",
)
MANAGED_FIELDS = frozenset({
    "id", "notes", "decisions", "legacy_path", "tombstone", "archived_at", "updated_at",
})
REQUIRED_FIELDS = frozenset({"id", "enabled", "session", "pane_id", "cwd", "policy"})
STUB_FIELDS = (
    "id", "enabled", "completed", "tombstone", "archived_at", "session",
    "workspace_id", "pane_id", "cwd", "project", "change", "updated_at",
)


class RegistryError(Exception):
    """Expected, user-facing failure. ``code`` is a stable machine string."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, Any]:
        return {"success": False, "error": self.code, "message": self.message}


# --------------------------------------------------------------------------
# paths / time
# --------------------------------------------------------------------------

def default_registry_path() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        raise RegistryError("no_hermes_home", "set HERMES_HOME or pass --registry")
    return Path(home) / "state" / "herdr_tasks.json"


def pipeline_project(registry: Path) -> str:
    """Project for new tasks from <HERMES_HOME>/herdr-pipeline.json ('' when not configured)."""
    try:
        data = json.loads((registry.parent.parent / "herdr-pipeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    project = data.get("project") if isinstance(data, dict) else None
    return project if isinstance(project, str) else ""


def lock_path(registry: Path) -> Path:
    return registry.with_name("herdr_tasks.lock")


def archive_path(registry: Path) -> Path:
    return registry.with_name("herdr_tasks_archive.jsonl")


def legacy_dir(registry: Path) -> Path:
    return registry.with_name("herdr_tasks_legacy")


def legacy_file(registry: Path, task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    if safe != task_id or safe.startswith("."):
        safe = f"{safe.lstrip('.') or 'task'}-{hashlib.sha256(task_id.encode()).hexdigest()[:8]}"
    return legacy_dir(registry) / f"{safe}.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# field validators (return the normalized value or raise RegistryError)
# --------------------------------------------------------------------------

def _bad(field: str, why: str) -> RegistryError:
    return RegistryError("invalid_field", f"{field}: {why}")


def _str(value: Any, field: str, max_len: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise _bad(field, "must be a string")
    text = value.strip()
    if "\x00" in text:
        raise _bad(field, "contains NUL")
    if len(text) > max_len:
        raise _bad(field, f"longer than {max_len} chars ({len(text)})")
    if required and not text:
        raise _bad(field, "must not be empty")
    return text


def _ident(value: Any, field: str, *, required: bool = True, max_len: int = 128) -> str:
    text = _str(value, field, max_len, required=required)
    if text and not ID_RE.fullmatch(text):
        raise _bad(field, "must match ^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$")
    return text


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(field, "must be true or false")
    return value


def _cwd(value: Any, field: str = "cwd") -> str:
    text = _str(value, field, 500, required=True)
    if not Path(text).is_absolute():
        raise _bad(field, "must be an absolute path")
    return text


def _changed_files(value: Any, field: str = "changed_files") -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise _bad(field, "must be a list of at most 100 relative paths")
    result: list[str] = []
    for raw in value:
        text = _str(raw, field, 500).replace("\\", "/")
        path = Path(text)
        if not text or path.is_absolute() or ".." in path.parts:
            raise _bad(field, f"unsafe path {text!r}")
        result.append(text)
    return result


def _risk_flags(value: Any, field: str = "risk_flags") -> dict[str, bool]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict) or len(value) > 50:
        raise _bad(field, "must be an object with at most 50 keys")
    result: dict[str, bool] = {}
    for key, enabled in value.items():
        name = _str(key, field, 64)
        if not RISK_KEY_RE.fullmatch(name):
            raise _bad(field, f"flag {name!r} must match [a-z][a-z0-9_]*")
        if bool(enabled):
            result[name] = True
    return dict(sorted(result.items()))


def _policy(value: Any, base: Any, field: str = "policy") -> dict[str, bool]:
    """Partial merge onto ``base``.  Keys missing from ``base`` take the event
    bridge's defaults, so a partial update never silently grants deploy/commit/
    push/sync-archive that the bridge treated as false."""
    if not isinstance(value, dict):
        raise _bad(field, "must be an object")
    unknown = sorted(set(value) - set(POLICY_KEYS))
    if unknown:
        raise _bad(field, f"unknown keys {unknown}; allowed {list(POLICY_KEYS)}")
    base = base if isinstance(base, dict) else {}
    merged = {key: bool(base.get(key, BRIDGE_POLICY_DEFAULTS[key])) for key in POLICY_KEYS}
    for key, flag in value.items():
        merged[key] = _bool(flag, f"policy.{key}")
    return merged


def _iso_utc(value: Any, field: str) -> str:
    text = _str(value, field, 40)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _bad(field, "must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise _bad(field, "must be UTC (suffix Z or +00:00)")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _wait(value: Any, field: str = "wait", task: dict[str, Any] | None = None) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise _bad(field, "must be an object {reason, until, note, quiet}")
    unknown = sorted(set(value) - {"reason", "until", "note", "quiet"})
    if unknown:
        raise _bad(field, f"unknown keys {unknown}")
    reason = _str(value.get("reason") or "none", "wait.reason", 32)
    if reason not in WAIT_REASONS:
        raise _bad("wait.reason", f"must be one of {list(WAIT_REASONS)}")
    until = _iso_utc(value.get("until"), "wait.until")
    if reason == "scheduled" and not until:
        raise _bad("wait.until", "required when reason=scheduled")
    quiet = value.get("quiet", False)
    if not isinstance(quiet, bool):
        raise _bad("wait.quiet", "must be a boolean")
    if quiet and not until:
        raise _bad("wait.quiet", "requires wait.until")
    # A reviewer waits on its author, whose events drive the workflow: no deadline.
    reviewer = bool(task and task.get("parent_task_id"))
    if not until and reason in RECHECK_SECONDS and not (reason == "external" and reviewer):
        at = datetime.now(timezone.utc) + timedelta(seconds=RECHECK_SECONDS[reason])
        until = at.isoformat(timespec="seconds").replace("+00:00", "Z")
    out: dict[str, Any] = {"reason": reason, "until": until, "note": _str(value.get("note"), "wait.note", 300)}
    # Only a long automated run opts in: the bridge then holds the agent's short progress turns until `until`.
    if quiet:
        out["quiet"] = True
    return out


def _generation(value: Any, current: Any, field: str = "phase_generation") -> int:
    if value == "+1":
        base = current if isinstance(current, int) and not isinstance(current, bool) else 0
        return base + 1
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise _bad(field, 'must be an integer 0..1000000 or "+1"')
    return value


def _sha(value: Any, field: str = "candidate_sha") -> str:
    text = _str(value, field, 64)
    if text and not SHA_RE.fullmatch(text):
        raise _bad(field, "must be 7-64 hex chars")
    return text.lower()


def _profile(value: Any, field: str = "execution_profile") -> str:
    text = _str(value, field, 32)
    if text and text not in EXECUTION_PROFILES:
        raise _bad(field, f"must be one of {list(EXECUTION_PROFILES)}")
    return text


def _compat(value: Any, field: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise _bad(field, "must be JSON-serializable") from exc
    if len(encoded) > MAX_COMPAT_JSON or "\x00" in encoded:
        raise _bad(field, f"serialized value must be <= {MAX_COMPAT_JSON} chars; use note/decision")
    return value


# field -> validator(value, task) for core fields writable through update/create
_VALIDATORS: dict[str, Callable[[Any, dict[str, Any]], Any]] = {
    "enabled": lambda v, t: _bool(v, "enabled"),
    "completed": lambda v, t: _bool(v, "completed"),
    "session": lambda v, t: _ident(v, "session", max_len=64),
    "workspace_id": lambda v, t: _str(v, "workspace_id", 128, required=True),
    "tab_id": lambda v, t: _ident(v, "tab_id", required=False),
    "pane_id": lambda v, t: _ident(v, "pane_id"),
    "agent": lambda v, t: _str(v, "agent", 64, required=True),
    "agent_session": lambda v, t: _str(v, "agent_session", 128),
    "previous_agent_session": lambda v, t: _str(v, "previous_agent_session", 128),
    "project": lambda v, t: _str(v, "project", 120, required=True),
    "cwd": lambda v, t: _cwd(v),
    "change": lambda v, t: _str(v, "change", 200),
    "phase": lambda v, t: _str(v, "phase", MAX_PHASE),
    "phase_generation": lambda v, t: _generation(v, t.get("phase_generation")),
    "branch": lambda v, t: _str(v, "branch", 200),
    "execution_profile": lambda v, t: _profile(v),
    "changed_files": lambda v, t: _changed_files(v),
    "risk_flags": lambda v, t: _risk_flags(v),
    "policy": lambda v, t: _policy(v, t.get("policy")),
    "candidate_sha": lambda v, t: _sha(v),
    "deploy_revision": lambda v, t: _str(v, "deploy_revision", 200),
    "evidence_manifest": lambda v, t: _str(v, "evidence_manifest", 500),
    "parent_task_id": lambda v, t: _ident(v, "parent_task_id", required=False, max_len=100),
    "parent_pane_id": lambda v, t: _ident(v, "parent_pane_id", required=False),
    "reviewer_pane_id": lambda v, t: _ident(v, "reviewer_pane_id", required=False),
    "reviewer_task_id": lambda v, t: _ident(v, "reviewer_task_id", required=False, max_len=100),
    "brief": lambda v, t: _str(v, "brief", MAX_BRIEF),
    "brief_path": lambda v, t: _str(v, "brief_path", 500),
    "kanban_task_id": lambda v, t: _str(v, "kanban_task_id", 128),
    "wait": lambda v, t: _wait(v, task=t),
}
UPDATABLE_FIELDS = tuple(sorted(set(_VALIDATORS) | set(COMPAT_FIELDS)))


def _entry(text: Any, who: Any, who_field: str) -> dict[str, str]:
    return {
        "at": now_iso(),
        who_field: _str(who or "controller", who_field, 64, required=True),
        "text": _str(text, "text", MAX_ENTRY_TEXT, required=True),
    }


# --------------------------------------------------------------------------
# bridge-compatible registry validation
# --------------------------------------------------------------------------

def _bridge_task_check(item: dict[str, Any]) -> None:
    """Mirror herdr_event_bridge.load_tasks for one enabled task."""
    task_id = item.get("id")
    label = f"task {task_id!r}"

    def need(value: Any, name: str, max_len: int) -> str:
        text = str(value or "").strip()
        if not text or len(text) > max_len or "\x00" in text:
            raise RegistryError("invalid_registry", f"{label}: invalid {name}")
        return text

    tid = need(task_id, "task id", 100)
    pane = need(item.get("pane_id"), "pane id", 128)
    session = need(item.get("session", "default"), "session", 64)
    if not (ID_RE.fullmatch(tid) and ID_RE.fullmatch(pane) and ID_RE.fullmatch(session)):
        raise RegistryError("invalid_registry", f"{label}: id, pane_id or session has unsupported characters")
    cwd = Path(need(item.get("cwd"), "cwd", 500))
    if not cwd.is_absolute():
        raise RegistryError("invalid_registry", f"{label}: cwd must be absolute")
    need(item.get("workspace_id", "unknown"), "workspace_id", 128)
    need(item.get("agent", "unknown"), "agent", 64)
    need(item.get("project", cwd.name), "project", 120)
    if not isinstance(item.get("policy") or {}, dict):
        raise RegistryError("invalid_registry", f"{label}: policy must be an object")
    try:
        _changed_files(item.get("changed_files"))
        _risk_flags(item.get("risk_flags"))
        declared = item.get("execution_profile")
        if declared:
            _profile(str(declared))
        _sha(str(item.get("candidate_sha") or ""))
        _str(str(item.get("deploy_revision") or ""), "deploy_revision", 200)
        manifest = _str(str(item.get("evidence_manifest") or ""), "evidence_manifest", 500)
    except RegistryError as exc:
        raise RegistryError("invalid_registry", f"{label}: {exc.message}") from exc
    if manifest:
        candidate = Path(manifest)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            candidate.resolve(strict=False).relative_to(cwd.resolve(strict=False))
        except ValueError as exc:
            raise RegistryError("invalid_registry", f"{label}: evidence_manifest must stay inside cwd") from exc


def validate_registry(data: Any) -> None:
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("tasks"), list):
        raise RegistryError("invalid_registry", "registry must be an object with version=1 and tasks[]")
    tasks = data["tasks"]
    if len(tasks) > MAX_TASKS:
        raise RegistryError("registry_full", f"more than {MAX_TASKS} tasks; archive or prune tombstones")
    decisions = data.get("decisions", [])
    if not isinstance(decisions, list) or len(decisions) > MAX_GLOBAL_DECISIONS:
        raise RegistryError("invalid_registry", f"top-level decisions must be a list of <= {MAX_GLOBAL_DECISIONS}")
    seen_ids: set[str] = set()
    enabled_panes: dict[str, str] = {}
    for item in tasks:
        if not isinstance(item, dict):
            raise RegistryError("invalid_registry", "every task must be an object")
        task_id = item.get("id")
        if not isinstance(task_id, str) or not ID_RE.fullmatch(task_id):
            raise RegistryError("invalid_registry", f"task id {task_id!r} is invalid")
        if task_id in seen_ids:
            raise RegistryError("invalid_registry", f"duplicate task id {task_id!r}")
        seen_ids.add(task_id)
        if item.get("enabled", True):
            _bridge_task_check(item)
            pane = str(item.get("pane_id"))
            if pane in enabled_panes:
                raise RegistryError(
                    "pane_in_use", f"pane {pane} is used by enabled tasks {enabled_panes[pane]!r} and {task_id!r}")
            enabled_panes[pane] = task_id


_BRIDGE_LOADER: Any = None  # tests may pin a callable here
_BRIDGE_CACHE: tuple[int, Any] | None = None


def _bridge_loader() -> Callable[[Path], Any] | None:
    """Return herdr_event_bridge.load_tasks from this scripts dir, if importable.

    Re-imported when the bridge file changes, so a long-lived gateway process
    validates with the same rules the running bridge will apply.
    """
    global _BRIDGE_CACHE
    if _BRIDGE_LOADER is not None:
        return _BRIDGE_LOADER or None
    path = Path(__file__).resolve().with_name("herdr_event_bridge.py")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    if _BRIDGE_CACHE is None or _BRIDGE_CACHE[0] != mtime:
        loader: Any = None
        try:
            spec = importlib.util.spec_from_file_location("_herdr_registry_bridge_check", path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            candidate = getattr(module, "load_tasks", None)
            loader = candidate if callable(candidate) else None
        except Exception:  # an unimportable bridge must not block registry writes
            loader = None
        _BRIDGE_CACHE = (mtime, loader)
    return _BRIDGE_CACHE[1]


# --------------------------------------------------------------------------
# locked IO
# --------------------------------------------------------------------------

@contextlib.contextmanager
def registry_lock(registry: Path, timeout: float = LOCK_TIMEOUT_S):
    path = lock_path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise RegistryError("lock_busy", f"could not acquire {path.name} within {timeout:.1f}s") from exc
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing releases the flock


def _read_bytes(registry: Path) -> bytes:
    try:
        with open(registry, "rb") as handle:
            raw = handle.read(MAX_READ_BYTES + 1)
    except FileNotFoundError:
        return b""
    if len(raw) > MAX_READ_BYTES:
        raise RegistryError("registry_too_large", f"{registry.name} exceeds {MAX_READ_BYTES} bytes")
    return raw


def _parse(raw: bytes) -> dict[str, Any]:
    if not raw.strip():
        return {"version": VERSION, "tasks": []}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError("invalid_registry", f"registry is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise RegistryError("invalid_registry", "registry must be an object with tasks[]")
    return data


def load(registry: Path | None = None) -> dict[str, Any]:
    """Unlocked snapshot read (atomic replace means readers never see partial files)."""
    return _parse(_read_bytes(Path(registry or default_registry_path())))


def _serialize(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _install(registry: Path, data: dict[str, Any], expected_raw: bytes) -> int:
    """Validate and atomically install ``data``; caller holds the lock."""
    validate_registry(data)
    encoded = _serialize(data)
    if len(encoded) > MAX_REGISTRY_BYTES:
        raise RegistryError("registry_too_large", f"result would be {len(encoded)} bytes (> {MAX_REGISTRY_BYTES})")
    registry.parent.mkdir(parents=True, exist_ok=True)
    temp = registry.with_name(f".{registry.name}.tmp-{os.getpid()}")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        loader = _bridge_loader()
        if loader is not None:
            try:
                loader(temp)
            except ValueError as exc:  # the loader's documented rejection type
                raise RegistryError("bridge_rejected", f"event bridge loader rejected the result: {exc}") from exc
            except Exception:  # incompatible/broken loader: our own validation already passed
                pass
        # Writers that bypass the lock (manual edits) must not be silently lost.
        if _read_bytes(registry) != expected_raw:
            raise RegistryError("concurrent_modification", "registry changed during the update; retry")
        os.replace(temp, registry)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
    return len(encoded)


def transaction(registry: Path | None, mutate: Callable[[dict[str, Any]], Any], *,
                lock_timeout: float = LOCK_TIMEOUT_S) -> Any:
    """Run ``mutate(data)`` under the lock and install the result.

    ``mutate`` returns the dict handed back to the caller; a truthy
    ``_no_write`` key in it (removed before returning) skips the write.
    A concurrent unlocked edit detected at install time retries once.
    """
    registry = Path(registry or default_registry_path())
    with registry_lock(registry, lock_timeout):
        for attempt in range(2):
            raw = _read_bytes(registry)
            data = _parse(raw)
            data.setdefault("version", VERSION)
            result = mutate(data)
            if isinstance(result, dict) and result.pop("_no_write", False):
                return result
            data["updated_at"] = now_iso()
            try:
                _install(registry, data, raw)
            except RegistryError as exc:
                if exc.code == "concurrent_modification" and attempt == 0:
                    continue
                raise
            return result
    raise RegistryError("concurrent_modification", "registry changed during the update; retry")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def is_stub(task: dict[str, Any]) -> bool:
    return bool(task.get("tombstone")) and bool(task.get("archived_at")) and not task.get("enabled", True)


def _find(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    task_id = _ident(task_id, "task_id", max_len=100)
    for task in data["tasks"]:
        if isinstance(task, dict) and task.get("id") == task_id:
            return task
    raise RegistryError("task_not_found", f"no task {task_id!r}; use list")


def _ordered(task: dict[str, Any]) -> dict[str, Any]:
    order = {key: index for index, key in enumerate(CORE_FIELDS + COMPAT_FIELDS)}
    return {key: task[key] for key in sorted(task, key=lambda k: (order.get(k, len(order)), k))}


def _stub(task: dict[str, Any], at: str) -> dict[str, Any]:
    stub = {key: task[key] for key in STUB_FIELDS if key in task}
    stub.update({"enabled": False, "completed": bool(task.get("completed", False)),
                 "tombstone": True, "archived_at": at, "updated_at": at})
    return _ordered(stub)


def _append_archive(registry: Path, task: dict[str, Any], at: str, reason: str) -> bool:
    """Append the full task unless an identical record is already archived."""
    path = archive_path(registry)
    digest = hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if path.exists():
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if digest in line and f'"{task.get("id")}"' in line:
                    return False
    line = json.dumps({"archived_at": at, "reason": reason, "sha256": digest, "task": task},
                      ensure_ascii=False, sort_keys=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _merge_legacy(registry: Path, task_id: str, moved: dict[str, Any], at: str) -> Path:
    path = legacy_file(registry, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    doc: dict[str, Any] = {"version": 1, "task_id": task_id, "fields": {}, "history": []}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("fields"), dict):
                doc.update(existing)
                doc.setdefault("history", [])
        except (OSError, json.JSONDecodeError):
            path.replace(path.with_suffix(f".corrupt-{int(time.time())}"))
    for key, value in moved.items():
        old = doc["fields"].get(key, None)
        if key in doc["fields"] and old != value:
            doc["history"].append({"key": key, "value": old, "replaced_at": at})
        doc["fields"][key] = value
    doc["history"] = doc["history"][-200:]
    doc["updated_at"] = at
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    os.replace(temp, path)
    return path


def _live_filter(live_panes: Iterable[str] | None) -> set[str] | None:
    return None if live_panes is None else {str(p) for p in live_panes}


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def view(task: dict[str, Any]) -> dict[str, Any]:
    """Compact view: core fields + wait + last notes/decisions + legacy workflow keys."""
    out = {key: task[key] for key in CORE_FIELDS if key in task and key not in ("notes", "decisions")}
    out["wait"] = task.get("wait") or {"reason": "none", "until": "", "note": ""}
    notes = task.get("notes") if isinstance(task.get("notes"), list) else []
    decisions = task.get("decisions") if isinstance(task.get("decisions"), list) else []
    out["notes"] = notes[-VIEW_TAIL:]
    out["decisions"] = decisions[-VIEW_TAIL:]
    if len(notes) > VIEW_TAIL:
        out["notes_total"] = len(notes)
    if len(decisions) > VIEW_TAIL:
        out["decisions_total"] = len(decisions)
    for key in COMPAT_FIELDS:
        if key in task:
            out[key] = task[key]
    extra = sorted(k for k in task if k not in CORE_FIELDS and k not in COMPAT_FIELDS)
    if extra:
        out["unmigrated_keys"] = extra
    return out


def get(task_id: str, registry: Path | None = None) -> dict[str, Any]:
    data = load(registry)
    if task_id == "*":
        decisions = data.get("decisions") if isinstance(data.get("decisions"), list) else []
        return {"success": True, "decisions": decisions[-10:], "decisions_total": len(decisions)}
    return {"success": True, "task": view(_find(data, task_id))}


def list_tasks(enabled_only: bool = True, registry: Path | None = None) -> dict[str, Any]:
    data = load(registry)
    rows = []
    for task in data["tasks"]:
        if not isinstance(task, dict):
            continue
        if enabled_only and (not task.get("enabled", True) or task.get("completed")):
            continue
        wait = task.get("wait") if isinstance(task.get("wait"), dict) else {}
        row = {
            "id": task.get("id"), "enabled": task.get("enabled", True),
            "completed": bool(task.get("completed", False)), "pane_id": task.get("pane_id"),
            "phase": str(task.get("phase") or "")[:80], "phase_generation": task.get("phase_generation"),
            "agent_session": task.get("agent_session", ""), "wait": wait.get("reason", "none"),
            "updated_at": task.get("updated_at", ""),
        }
        if is_stub(task):
            row["tombstone"] = True
        rows.append(row)
    decisions = data.get("decisions") if isinstance(data.get("decisions"), list) else []
    return {"success": True, "count": len(rows), "total": len(data["tasks"]), "tasks": rows,
            "global_decisions": decisions[-VIEW_TAIL:]}


def _apply_fields(task: dict[str, Any], fields: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for key, value in fields.items():
        if key in MANAGED_FIELDS:
            raise RegistryError("managed_field", f"{key} is managed; use note/decision/archive actions")
        if key not in _VALIDATORS and key not in COMPAT_FIELDS:
            raise RegistryError("unknown_field", f"unknown field {key!r}; allowed: {', '.join(UPDATABLE_FIELDS)}")
        if value is None:
            if key in REQUIRED_FIELDS:
                raise _bad(key, "is required and cannot be removed")
            if key in task:
                del task[key]
                changed.append(key)
            continue
        normalized = _compat(value, key) if key in COMPAT_FIELDS else _VALIDATORS[key](value, task)
        if task.get(key) != normalized:
            task[key] = normalized
            changed.append(key)
    return changed


def update(task_id: str, fields: dict[str, Any], by: str = "controller", *,
           expect_generation: int | None = None, registry: Path | None = None) -> dict[str, Any]:
    if not isinstance(fields, dict) or not fields:
        raise RegistryError("invalid_request", "fields must be a non-empty object")
    by = _str(by or "controller", "by", 64, required=True)

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        task = _find(data, task_id)
        if is_stub(task):
            raise RegistryError("task_archived", f"{task_id} is archived; create a new task")
        current = task.get("phase_generation") or 0
        if expect_generation is not None and current != expect_generation:
            raise RegistryError("generation_conflict",
                                f"phase_generation is {current}, expected {expect_generation}; re-read the task")
        changed = _apply_fields(task, fields)
        if changed:
            task["updated_at"] = now_iso()
        result = {"success": True, "task_id": task_id, "changed": changed, "by": by,
                  "phase_generation": task.get("phase_generation"), "_no_write": not changed}
        if "wait" in changed and task["wait"]["reason"] == "user_decision" and by == "controller":
            result["reminder"] = OWNER_QUESTION_REMINDER
        return result

    return transaction(registry, mutate)


def add_note(task_id: str, text: str, by: str = "controller", registry: Path | None = None) -> dict[str, Any]:
    entry = _entry(text, by, "by")

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        task = _find(data, task_id)
        notes = task.get("notes") if isinstance(task.get("notes"), list) else []
        notes.append(entry)
        task["notes"] = notes[-MAX_NOTES:]
        task["updated_at"] = entry["at"]
        return {"success": True, "task_id": task_id, "note": entry, "notes_total": len(task["notes"])}

    return transaction(registry, mutate)


def add_decision(task_id: str, text: str, source: str = "controller",
                 registry: Path | None = None) -> dict[str, Any]:
    entry = _entry(text, source, "source")

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        if task_id == "*":
            decisions = data.get("decisions") if isinstance(data.get("decisions"), list) else []
            decisions.append(entry)
            data["decisions"] = decisions[-MAX_GLOBAL_DECISIONS:]
            return {"success": True, "task_id": "*", "decision": entry, "decisions_total": len(data["decisions"])}
        task = _find(data, task_id)
        decisions = task.get("decisions") if isinstance(task.get("decisions"), list) else []
        decisions.append(entry)
        task["decisions"] = decisions[-MAX_TASK_DECISIONS:]
        task["updated_at"] = entry["at"]
        return {"success": True, "task_id": task_id, "decision": entry, "decisions_total": len(task["decisions"])}

    return transaction(registry, mutate)


def create(task: dict[str, Any], by: str = "controller", registry: Path | None = None) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise RegistryError("invalid_request", "task must be an object")
    fields = dict(task)
    task_id = _ident(fields.pop("id", None), "id", max_len=100)
    for key in ("pane_id", "cwd"):
        if not fields.get(key):
            raise _bad(key, "is required for create")
    registry = Path(registry or default_registry_path())

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        if any(isinstance(t, dict) and t.get("id") == task_id for t in data["tasks"]):
            raise RegistryError("task_exists", f"task {task_id!r} already exists")
        if len(data["tasks"]) >= MAX_TASKS:
            raise RegistryError("registry_full", f"{MAX_TASKS} tasks; archive or prune tombstones first")
        cwd = _cwd(fields.get("cwd"))
        pane = _ident(fields.get("pane_id"), "pane_id")
        same_cwd = [t.get("project") for t in data["tasks"] if isinstance(t, dict) and t.get("cwd") == cwd and t.get("project")]
        workspace = pane.split(":", 1)[0] if ":" in pane else "unknown"
        new: dict[str, Any] = {
            "id": task_id, "enabled": True, "completed": False, "session": "default",
            "workspace_id": workspace or "unknown", "pane_id": pane, "agent": "claude",
            "project": same_cwd[-1] if same_cwd else (pipeline_project(registry) or Path(cwd).name or "project"),
            "cwd": cwd,
            "change": "", "phase": "new", "phase_generation": 0, "branch": "",
            "execution_profile": "standard", "changed_files": [], "risk_flags": {},
            "policy": {key: True for key in POLICY_KEYS},
            "wait": {"reason": "none", "until": "", "note": ""},
        }
        _apply_fields(new, fields)
        # The profile's pipeline owns the project name: the route script matches it
        # exactly, so a caller-supplied variant would silently drop every event.
        new["project"] = pipeline_project(registry) or new["project"]
        new["updated_at"] = now_iso()
        data["tasks"].append(_ordered(new))
        return {"success": True, "task_id": task_id, "by": _str(by or "controller", "by", 64), "task": view(new)}

    return transaction(registry, mutate)


def archive(task_id: str, live_panes: Iterable[str] | None = None, reason: str = "archived",
            registry: Path | None = None) -> dict[str, Any]:
    """Move a task's full record to the archive jsonl, leaving a tombstone stub.

    With ``live_panes`` (ids from ``herdr pane list``), stubs whose pane is gone
    are dropped instead of kept -- for this task and all other stubs.
    """
    registry = Path(registry or default_registry_path())
    live = _live_filter(live_panes)
    reason = _str(reason or "archived", "reason", 200, required=True)

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        task = _find(data, task_id)
        at = now_iso()
        result: dict[str, Any] = {"success": True, "task_id": task_id}
        if is_stub(task):
            result["already_archived"] = True
        else:
            result["archived"] = _append_archive(registry, copy.deepcopy(task), at, reason)
            index = data["tasks"].index(task)
            data["tasks"][index] = _stub(task, at)
        result["pruned_tombstones"] = prune_stubs(data, live)
        result["tombstone_kept"] = any(t.get("id") == task_id for t in data["tasks"] if isinstance(t, dict))
        return result

    return transaction(registry, mutate)


def prune_stubs(data: dict[str, Any], live: set[str] | None) -> list[str]:
    if live is None:
        return []
    pruned = [t.get("id") for t in data["tasks"] if isinstance(t, dict) and is_stub(t) and t.get("pane_id") not in live]
    data["tasks"] = [t for t in data["tasks"] if not (isinstance(t, dict) and t.get("id") in pruned and is_stub(t))]
    return pruned


def migrate(dry_run: bool = True, live_panes: Iterable[str] | None = None,
            registry: Path | None = None, lock_timeout: float = 1.0) -> dict[str, Any]:
    """Shrink the registry to core (+ compat) keys.  Idempotent.

    * non-core keys -> ``herdr_tasks_legacy/<id>.json`` (merged), ``legacy_path`` set
    * ``brief`` > 2000 / ``phase`` > 300 chars truncated (full text kept in legacy)
    * compat keys kept unless their JSON is > 2000 chars (then moved to legacy)
    * disabled or completed tasks -> archive jsonl + tombstone stub
      (stub dropped when ``live_panes`` is given and the pane is gone)
    """
    registry = Path(registry or default_registry_path())
    live = _live_filter(live_panes)
    context = contextlib.nullcontext() if dry_run else registry_lock(registry, lock_timeout)
    with context:
        raw = _read_bytes(registry)
        data = _parse(raw)
        at = now_iso()
        new_tasks: list[dict[str, Any]] = []
        archive_records: list[dict[str, Any]] = []
        legacy_moves: dict[str, dict[str, Any]] = {}
        report: list[dict[str, Any]] = []
        for task in data["tasks"]:
            if not isinstance(task, dict):
                report.append({"id": None, "action": "dropped_non_object"})
                continue
            task_id = str(task.get("id"))
            if is_stub(task):
                if live is not None and task.get("pane_id") not in live:
                    report.append({"id": task_id, "action": "pruned_tombstone"})
                else:
                    new_tasks.append(task)
                continue
            if not task.get("enabled", True) or task.get("completed") is True:
                archive_records.append(copy.deepcopy(task))
                keep = live is None or task.get("pane_id") in live
                if keep:
                    new_tasks.append(_stub(task, at))
                report.append({"id": task_id, "action": "archived", "tombstone_kept": keep,
                               "bytes": len(json.dumps(task, ensure_ascii=False))})
                continue
            moved: dict[str, Any] = {}
            kept: dict[str, Any] = {}
            for key, value in task.items():
                if key in CORE_FIELDS:
                    kept[key] = value
                elif key in COMPAT_FIELDS and len(json.dumps(value, ensure_ascii=False)) <= MAX_COMPAT_JSON:
                    kept[key] = value
                else:
                    moved[key] = value
            for key, limit in (("brief", MAX_BRIEF), ("phase", MAX_PHASE)):
                value = kept.get(key)
                if isinstance(value, str) and len(value) > limit:
                    moved[f"{key}_full"] = value
                    kept[key] = value[:limit]
            if moved:
                legacy_moves[task_id] = moved
                kept["legacy_path"] = str(legacy_file(registry, task_id))
            new_tasks.append(_ordered(kept))
            if moved:
                report.append({"id": task_id, "action": "slimmed", "moved_keys": sorted(moved),
                               "bytes_before": len(json.dumps(task, ensure_ascii=False)),
                               "bytes_after": len(json.dumps(kept, ensure_ascii=False))})
            else:
                report.append({"id": task_id, "action": "unchanged"})
        new_data = dict(data)
        new_data["tasks"] = new_tasks
        new_data.setdefault("version", VERSION)
        changed = bool(archive_records or legacy_moves) or len(new_tasks) != len(data["tasks"])
        after_bytes = len(_serialize(new_data)) if changed else len(raw)
        summary: dict[str, Any] = {
            "success": True, "dry_run": dry_run, "changed": changed,
            "bytes_before": len(raw), "bytes_after": after_bytes,
            "tasks_before": len(data["tasks"]), "tasks_after": len(new_tasks),
            "active_tasks": sum(1 for t in new_tasks if t.get("enabled", True) and not t.get("completed")),
            "tombstones": sum(1 for t in new_tasks if is_stub(t)),
            "archived": len(archive_records), "slimmed": len(legacy_moves),
            "tasks": report,
        }
        if dry_run or not changed:
            return summary
        new_data["updated_at"] = at
        validate_registry(new_data)  # fail before touching sidecars
        for task_id, moved in legacy_moves.items():
            _merge_legacy(registry, task_id, moved, at)
        for record in archive_records:
            _append_archive(registry, record, at, "migrate")
        summary["bytes_after"] = _install(registry, new_data, raw)
        summary["archive_path"] = str(archive_path(registry))
        summary["legacy_dir"] = str(legacy_dir(registry))
        return summary


def live_panes_from_json(text: str) -> set[str]:
    """Parse ``herdr pane list`` JSON output into a set of pane ids."""
    data = json.loads(text)
    panes = data.get("result", {}).get("panes") if isinstance(data, dict) else None
    if not isinstance(panes, list):
        raise RegistryError("invalid_request", "live panes JSON has no result.panes[]")
    return {str(p.get("pane_id")) for p in panes if isinstance(p, dict) and p.get("pane_id")}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", type=Path, default=None,
                        help="default: $HERMES_HOME/state/herdr_tasks.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="compact task view, or list when no id")
    show.add_argument("id", nargs="?")
    lst = sub.add_parser("list")
    lst.add_argument("--all", action="store_true", help="include disabled/tombstoned tasks")
    mig = sub.add_parser("migrate")
    mig.add_argument("--apply", action="store_true")
    mig.add_argument("--live-panes", type=Path, help="saved `herdr pane list` JSON; prunes stubs of closed panes")
    note = sub.add_parser("note")
    note.add_argument("id"); note.add_argument("text"); note.add_argument("--by", default="operator")
    dec = sub.add_parser("decision")
    dec.add_argument("id", help="task id or '*' for global"); dec.add_argument("text")
    dec.add_argument("--source", default="operator")
    upd = sub.add_parser("update")
    upd.add_argument("id"); upd.add_argument("json"); upd.add_argument("--by", default="operator")
    upd.add_argument("--expect-generation", type=int)
    crt = sub.add_parser("create")
    crt.add_argument("json")
    arc = sub.add_parser("archive")
    arc.add_argument("id")
    arc.add_argument("--live-panes", type=Path)
    args = parser.parse_args(argv)
    reg = args.registry or default_registry_path()

    def live() -> set[str] | None:
        path = getattr(args, "live_panes", None)
        return live_panes_from_json(path.read_text(encoding="utf-8")) if path else None

    try:
        if args.cmd == "show":
            result = get(args.id, reg) if args.id else list_tasks(True, reg)
        elif args.cmd == "list":
            result = list_tasks(not args.all, reg)
        elif args.cmd == "migrate":
            result = migrate(dry_run=not args.apply, live_panes=live(), registry=reg)
        elif args.cmd == "note":
            result = add_note(args.id, args.text, args.by, reg)
        elif args.cmd == "decision":
            result = add_decision(args.id, args.text, args.source, reg)
        elif args.cmd == "update":
            result = update(args.id, json.loads(args.json), args.by,
                            expect_generation=args.expect_generation, registry=reg)
        elif args.cmd == "create":
            result = create(json.loads(args.json), "operator", reg)
        else:
            result = archive(args.id, live(), registry=reg)
    except RegistryError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 1
    except (json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"success": False, "error": "invalid_request", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
