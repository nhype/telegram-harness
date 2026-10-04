#!/usr/bin/env python3
"""Event-driven Herdr -> Hermes webhook bridge (v2).

Subscribes to Herdr's newline-delimited JSON Unix socket API and wakes a Hermes
controller when a registered coding agent needs attention.

v2 behaviour (see ``Bridge``):
- per-task debounce (idle/done 20 s, blocked 3 s, unknown 45 s grace; latest
  status wins, working cancels) and a hold that suppresses repeat wakes when
  nothing has worked since the last delivery;
- per-workflow single-flight: while a controller run for any task of a
  workflow (author + linked reviewer) is in flight, further wakes are
  deferred and re-evaluated once the gateway's ``state.db`` shows the run
  ended;
- a deterministic watchdog for stalls, elapsed registry ``wait.until`` and
  working panes that produce no output;
- a non-blocking delivery worker with bounded retries, so socket reading and
  registry reloads are never starved.

No raw pane output or secrets are included in webhook payloads: pane text is
only classified into a fixed ``hint`` label.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

# The bridge is also loaded by path (registry validation, route script): find its sibling module.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import webhook_ids  # noqa: E402


def default_socket() -> str:
    """Herdr's API socket for the current user ($XDG_CONFIG_HOME/herdr/herdr.sock)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "herdr.sock")


ID_RE =re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
STATUS_WAKE = {"idle", "blocked", "done", "unknown"}
STATUS_ALL = STATUS_WAKE | {"working"}
IDLE_LIKE = frozenset({"idle", "done", "unknown"})
MAX_TASKS = 100
MAX_PENDING = 500
EXECUTION_PROFILES = {"micro", "standard", "high-risk", "research"}
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
MAX_EVIDENCE_BYTES = 1_048_576

EVENT_TYPE = "herdr.agent_status_changed"
WAKE_REASONS = ("status", "stall", "wait_elapsed", "working_no_output", "deferred")
WAIT_REASONS = frozenset({"none", "user_decision", "external", "scheduled", "complete"})
# `external` without `until`: the linked side (author/reviewer) drives the next wake, not the watchdog.
WAIT_SUPPRESSES_STALL = frozenset({"user_decision", "complete", "external"})
RETRY_BACKOFF = (5, 15, 45, 120, 300)
# A working stretch shorter than this during an active wait is a progress ping, not a result.
SHORT_TURN_SECONDS = 60.0
MAX_WEBHOOK_TIMEOUT = 45
MAX_RECENT_WORK = 8
MAX_SOCKET_BUFFER = 4 * 1024 * 1024
NO_HINT = {"kind": "none", "label": ""}
NO_WAIT = {"reason": "none", "until": ""}


# ---------------------------------------------------------------------------
# Routing / registry helpers (public; imported by the route script and tests)
# ---------------------------------------------------------------------------


def cwd_matches(cwd: str, prefixes: list[str], sibling_prefix: bool = False) -> bool:
    """Return whether a pane cwd belongs to this project route."""
    value = os.path.normpath(str(cwd or "").removesuffix(" (deleted)"))
    for raw_prefix in prefixes:
        prefix = os.path.normpath(raw_prefix)
        if value == prefix or value.startswith(prefix + os.sep):
            return True
        if sibling_prefix and value.startswith(prefix + "-"):
            return True
    return False


PIPELINE_CONFIG = "herdr-pipeline.json"


def load_pipeline_config(path: Path | str) -> dict[str, Any] | None:
    """Per-profile project settings (<HERMES_HOME>/herdr-pipeline.json); None if absent or invalid."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not all(isinstance(data.get(key), str) and data[key] for key in ("profile", "project", "route")):
        return None
    prefixes = data.get("cwd_prefixes")
    if (not isinstance(prefixes, list) or not prefixes
            or not all(isinstance(p, str) and p.startswith("/") for p in prefixes)):
        return None
    return {**data, "sibling_prefix": bool(data.get("sibling_prefix", False))}


def merge_discovered_panes(
    registered: dict[str, dict[str, Any]],
    panes: list[dict[str, Any]],
    *,
    project: str,
    session: str,
    cwd_prefixes: list[str],
    sibling_prefix: bool,
    tombstoned_panes: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Route live panes by cwd, preserving registry metadata where available."""
    registered_by_pane = {task["pane_id"]: task for task in registered.values()}
    tombstoned_panes = tombstoned_panes or set()
    tasks: dict[str, dict[str, Any]] = {}
    for pane in panes:
        pane_id = str(pane.get("pane_id") or "")
        cwd = str(pane.get("cwd") or pane.get("foreground_cwd") or "")
        if not pane_id or not cwd_matches(cwd, cwd_prefixes, sibling_prefix):
            continue
        existing = registered_by_pane.get(pane_id)
        if existing:
            tasks[existing["id"]] = existing
            continue
        # A disabled registry record is a durable tombstone.  Do not resurrect
        # its still-live pane as auto:<pane> while graceful /exit is finishing.
        if pane_id in tombstoned_panes:
            continue
        task_id = f"auto:{pane_id}"
        tasks[task_id] = {
            "id": task_id,
            "session": session,
            "workspace_id": str(pane.get("workspace_id") or "unknown")[:128],
            "pane_id": pane_id,
            "agent": str(pane.get("agent") or "unknown")[:64],
            "project": project,
            "cwd": cwd.removesuffix(" (deleted)"),
            "change": "",
            "phase": "auto-discovered by pane cwd; no automatic continuation",
            "branch": "",
            "execution_profile": "research",
            "changed_files": [],
            "risk_flags": {},
            "evidence_manifest": "",
            "candidate_sha": "",
            "deploy_revision": "",
            "policy": {
                "allow_continue": False,
                "allow_tests": False,
                "allow_deploy": False,
                "allow_commit": False,
                "allow_push": False,
                "allow_sync_archive": False,
            },
        }
    return tasks


def list_herdr_panes(herdr_bin: str, session: str, timeout: int = 10) -> list[dict[str, Any]]:
    """Read the live pane inventory using an explicit Herdr session."""
    proc = subprocess.run(
        [herdr_bin, "--session", session, "pane", "list"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Herdr pane list failed rc={proc.returncode}")
    data = json.loads(proc.stdout)
    panes = data.get("result", {}).get("panes")
    if not isinstance(panes, list):
        raise ValueError("Herdr pane list response has no panes[]")
    return [pane for pane in panes if isinstance(pane, dict)]


def herdr_agent_info(herdr_bin: str, session: str, pane_id: str, timeout: int = 5) -> dict[str, Any]:
    """Return ``result.agent`` from ``herdr agent get`` (read-only)."""
    proc = subprocess.run(
        [herdr_bin, "--session", session, "agent", "get", pane_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Herdr agent get failed rc={proc.returncode}")
    agent = json.loads(proc.stdout).get("result", {}).get("agent")
    if not isinstance(agent, dict):
        raise ValueError("Herdr agent get response has no agent")
    return agent


def read_pane_tail(herdr_bin: str, session: str, pane_id: str, lines: int = 40, timeout: int = 5) -> str:
    """Read recent pane text for local hint classification only (never forwarded)."""
    proc = subprocess.run(
        [herdr_bin, "--session", session, "pane", "read", pane_id,
         "--source", "recent-unwrapped", "--lines", str(lines)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Herdr pane read failed rc={proc.returncode}")
    return proc.stdout or ""


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{stamp} {message}", flush=True)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be an object")
    return data


def clean_text(value: Any, name: str, max_len: int = 300) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_len or "\x00" in text:
        raise ValueError(f"invalid {name}")
    return text


def optional_text(value: Any, name: str, max_len: int = 300) -> str:
    text = str(value or "").strip()
    if len(text) > max_len or "\x00" in text:
        raise ValueError(f"invalid {name}")
    return text


def normalize_changed_files(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("changed_files must be a list of at most 100 paths")
    result: list[str] = []
    for raw in value:
        text = optional_text(raw, "changed file", 500).replace("\\", "/")
        path = Path(text)
        if not text or path.is_absolute() or ".." in path.parts:
            raise ValueError("changed_files must contain safe relative paths")
        result.append(text)
    return result


def normalize_risk_flags(value: Any) -> dict[str, bool]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError("risk_flags must be an object with at most 50 keys")
    result: dict[str, bool] = {}
    for raw_key, enabled in value.items():
        key = optional_text(raw_key, "risk flag", 64)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise ValueError("risk flag contains unsupported characters")
        if bool(enabled):
            result[key] = True
    return dict(sorted(result.items()))


def micro_safe_file(path: str) -> bool:
    lowered = path.lower()
    suffix = Path(lowered).suffix
    if suffix in {".html", ".css", ".scss", ".sass", ".less", ".md", ".txt", ".svg", ".png", ".jpg", ".jpeg", ".webp"}:
        return True
    if suffix in {".json", ".yaml", ".yml"} and lowered.startswith("html/"):
        return True
    return suffix == ".js" and ("/locales/" in lowered or lowered.startswith("html/js/locales/"))


def classify_execution_profile(
    declared: Any,
    *,
    changed_files: list[str],
    risk_flags: dict[str, bool],
    phase: str,
    policy: dict[str, bool],
) -> str:
    profile = optional_text(declared, "execution profile", 32) if declared else ""
    if profile and profile not in EXECUTION_PROFILES:
        raise ValueError(f"unsupported execution_profile: {profile}")
    if risk_flags:
        return "high-risk"

    mutating = any(
        policy.get(key, False)
        for key in ("allow_continue", "allow_deploy", "allow_commit", "allow_push", "allow_sync_archive")
    )
    phase_lower = phase.lower()
    if not mutating and any(marker in phase_lower for marker in ("read-only", "explore", "audit")):
        return "research"

    static_micro = 0 < len(changed_files) <= 4 and all(micro_safe_file(path) for path in changed_files)
    if profile == "high-risk":
        return profile
    if profile == "research":
        return "research" if not mutating else "standard"
    if profile == "standard":
        return profile
    if profile == "micro":
        return "micro" if static_micro else "standard"
    return "micro" if static_micro else "standard"


def normalize_evidence_path(value: Any, cwd: Path) -> str:
    text = optional_text(value, "evidence manifest", 500)
    if not text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(cwd.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("evidence_manifest must stay inside task cwd") from exc
    return str(candidate)


def normalize_registry(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate a raw registry object and return enabled tasks in wire shape.

    Raises ValueError on any invalid content.  The returned task dicts are the
    exact shape hashed by ``task_fingerprint`` (the route script relies on it).
    """
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("tasks"), list):
        raise ValueError("registry must contain version=1 and tasks[]")
    if len(raw["tasks"]) > MAX_TASKS:
        raise ValueError("registry has too many tasks")

    tasks: dict[str, dict[str, Any]] = {}
    for item in raw["tasks"]:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        task_id = clean_text(item.get("id"), "task id", 100)
        pane_id = clean_text(item.get("pane_id"), "pane id", 128)
        session = clean_text(item.get("session", "default"), "session", 64)
        if not ID_RE.fullmatch(task_id) or not ID_RE.fullmatch(pane_id) or not ID_RE.fullmatch(session):
            raise ValueError("task id, pane id, or session contains unsupported characters")
        if task_id in tasks:
            raise ValueError(f"duplicate task id: {task_id}")
        cwd = Path(clean_text(item.get("cwd"), "cwd", 500))
        if not cwd.is_absolute():
            raise ValueError(f"task {task_id}: cwd must be absolute")
        policy = item.get("policy") or {}
        if not isinstance(policy, dict):
            raise ValueError(f"task {task_id}: policy must be an object")
        normalized_policy = {
            "allow_continue": bool(policy.get("allow_continue", True)),
            "allow_tests": bool(policy.get("allow_tests", True)),
            "allow_deploy": bool(policy.get("allow_deploy", False)),
            "allow_commit": bool(policy.get("allow_commit", False)),
            "allow_push": bool(policy.get("allow_push", False)),
            "allow_sync_archive": bool(policy.get("allow_sync_archive", False)),
        }
        changed_files = normalize_changed_files(item.get("changed_files"))
        risk_flags = normalize_risk_flags(item.get("risk_flags"))
        phase = str(item.get("phase") or "unknown").strip()[:80]
        candidate_sha = optional_text(item.get("candidate_sha"), "candidate sha", 64)
        if candidate_sha and not SHA_RE.fullmatch(candidate_sha):
            raise ValueError(f"task {task_id}: invalid candidate_sha")
        task = {
            "id": task_id,
            "session": session,
            "workspace_id": clean_text(item.get("workspace_id", "unknown"), "workspace id", 128),
            "pane_id": pane_id,
            "agent": clean_text(item.get("agent", "unknown"), "agent", 64),
            "project": clean_text(item.get("project", cwd.name), "project", 120),
            "cwd": str(cwd),
            "change": str(item.get("change") or "").strip()[:200],
            "phase": phase,
            "branch": str(item.get("branch") or "").strip()[:200],
            "execution_profile": classify_execution_profile(
                item.get("execution_profile"),
                changed_files=changed_files,
                risk_flags=risk_flags,
                phase=phase,
                policy=normalized_policy,
            ),
            "changed_files": changed_files,
            "risk_flags": risk_flags,
            "evidence_manifest": normalize_evidence_path(item.get("evidence_manifest"), cwd),
            "candidate_sha": candidate_sha.lower(),
            "deploy_revision": optional_text(item.get("deploy_revision"), "deploy revision", 200),
            "policy": normalized_policy,
        }
        tasks[task_id] = task
    return tasks


def load_tasks(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    """Return (enabled tasks in wire shape, registry mtime_ns); ValueError if invalid."""
    raw = load_json(path, {"version": 1, "tasks": []})
    tasks = normalize_registry(raw)
    return tasks, path.stat().st_mtime_ns if path.exists() else 0


def tombstones_from_raw(raw: dict[str, Any]) -> set[str]:
    """Pane ids of every ``enabled: false`` record (incl. archive stubs)."""
    panes: set[str] = set()
    for item in raw.get("tasks", []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict) or item.get("enabled", True):
            continue
        pane_id = str(item.get("pane_id") or "").strip()
        if pane_id and ID_RE.fullmatch(pane_id):
            panes.add(pane_id)
    return panes


def load_tombstoned_panes(path: Path) -> set[str]:
    """Return pane ids explicitly disabled in the durable registry."""
    return tombstones_from_raw(load_json(path, {"version": 1, "tasks": []}))


def parse_time(value: Any) -> float | None:
    """Parse an epoch number/string or ISO-8601 timestamp to epoch seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
        return number if number > 0 else None
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def normalize_wait(value: Any) -> dict[str, Any]:
    """Return the payload ``wait`` shape: {reason, until(ISO UTC or "")}, plus quiet=True if declared."""
    if not isinstance(value, dict):
        return dict(NO_WAIT)
    reason = str(value.get("reason") or "none").strip().lower()
    if reason not in WAIT_REASONS:
        reason = "none"
    until = parse_time(value.get("until"))
    out: dict[str, Any] = {"reason": reason, "until": iso_time(until) if until else ""}
    if value.get("quiet") is True:
        out["quiet"] = True
    return out


def registry_extras(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Raw per-task fields the bridge needs but that are NOT part of the wire task.

    ``wait`` and ``parent_task_id`` deliberately stay out of the normalized
    task so that ``task_fingerprint`` semantics are unchanged.
    """
    extras: dict[str, dict[str, Any]] = {}
    for item in raw.get("tasks", []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id") or "").strip()
        if not task_id or (task_id in extras and not item.get("enabled", True)):
            continue
        parent = str(item.get("parent_task_id") or "").strip()
        extras[task_id] = {
            "wait": normalize_wait(item.get("wait")),
            "parent_task_id": parent if parent and ID_RE.fullmatch(parent) else "",
        }
    return extras


def task_fingerprint(task: dict[str, Any]) -> str:
    """Return a stable revision used to invalidate superseded wake events."""
    canonical = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evidence_status(task: dict[str, Any]) -> dict[str, Any]:
    """Return non-secret freshness metadata without forwarding manifest content."""
    raw_path = str(task.get("evidence_manifest") or "").strip()
    if not raw_path:
        return {"status": "not_configured"}
    path = Path(raw_path)
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(Path(str(task.get("cwd") or "")).resolve(strict=False))
    except (OSError, ValueError):
        return {"status": "invalid", "reasons": ["manifest_outside_cwd"]}
    path = resolved
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError as exc:
        return {"status": "invalid", "reasons": [type(exc).__name__]}
    if stat.st_size > MAX_EVIDENCE_BYTES:
        return {"status": "invalid", "reasons": ["manifest_too_large"]}
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "invalid", "reasons": [type(exc).__name__]}
    if not isinstance(manifest, dict):
        return {"status": "invalid", "reasons": ["manifest_not_object"]}

    reasons: list[str] = []
    expected_sha = str(task.get("candidate_sha") or "").lower()
    manifest_sha = str(manifest.get("candidate_sha") or manifest.get("commit") or "").lower()
    if expected_sha and manifest_sha != expected_sha:
        reasons.append("candidate_sha_mismatch")
    expected_revision = str(task.get("deploy_revision") or "")
    manifest_revision = str(manifest.get("deploy_revision") or "")
    if expected_revision and manifest_revision != expected_revision:
        reasons.append("deploy_revision_mismatch")
    if reasons:
        return {"status": "stale", "reasons": reasons}
    return {
        "status": "current",
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": stat.st_size,
    }


def load_state(path: Path) -> dict[str, Any]:
    """Load the bridge state file (version 1); tolerates v1 files without v2 keys."""
    state = load_json(path, {"version": 1, "panes": {}, "pending": []})
    if state.get("version") != 1:
        raise ValueError("unsupported state version")
    state.setdefault("panes", {})
    state.setdefault("pending", [])
    if not isinstance(state["panes"], dict) or not isinstance(state["pending"], list):
        raise ValueError("invalid state structure")
    state["pending"] = state["pending"][-MAX_PENDING:]
    if not isinstance(state.get("queue"), list):
        state["queue"] = []
    if not isinstance(state.get("workflows"), dict):
        state["workflows"] = {}
    return state


def subscription_request(tasks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    topology = [
        {"type": "pane.created"},
        {"type": "pane.updated"},
        {"type": "pane.closed"},
        {"type": "pane.agent_detected"},
    ]
    return {
        "id": f"hermes-bridge-{os.getpid()}",
        "method": "events.subscribe",
        "params": {
            "subscriptions": topology + [
                {"type": "pane.agent_status_changed", "pane_id": task["pane_id"]}
                for task in tasks.values()
            ]
        },
    }


def is_topology_event(message: dict[str, Any]) -> bool:
    event = str(message.get("event") or "")
    return event in {
        "pane_created",
        "pane_updated",
        "pane_closed",
        "pane_agent_detected",
        "pane.created",
        "pane.updated",
        "pane.closed",
        "pane.agent_detected",
    }


def topology_requires_rebuild(
    current: dict[str, dict[str, Any]], refreshed: dict[str, dict[str, Any]]
) -> bool:
    current_panes = {task["pane_id"] for task in current.values()}
    refreshed_panes = {task["pane_id"] for task in refreshed.values()}
    return current_panes != refreshed_panes


# ---------------------------------------------------------------------------
# Status tracking and payload contract
# ---------------------------------------------------------------------------


def record_status(
    pane: dict[str, Any],
    task: dict[str, Any],
    status: str,
    now: float,
    *,
    min_work_seconds: float = 20.0,
) -> bool:
    """Record a status change in per-task state; return False if unchanged."""
    previous = pane.get("last_seen")
    if previous == status:
        return False
    observed_at = iso_time(now)
    previous_since = pane.get("status_since")
    if previous and previous_since is None:
        previous_since = parse_time(pane.get("status_started_at"))
    duration = max(0.0, now - float(previous_since)) if previous and previous_since is not None else None

    phase = str(task.get("phase") or "unknown")
    if pane.get("phase") != phase:
        old_phase = pane.get("phase")
        old_started = parse_time(pane.get("phase_started_at"))
        if old_phase and old_started is not None:
            durations = list(pane.get("phase_durations") or [])
            durations.append({"phase": old_phase, "duration_seconds": max(0.0, now - old_started)})
            pane["phase_durations"] = durations[-20:]
        pane["phase"] = phase
        pane["phase_started_at"] = observed_at

    pane["prev_status"] = previous
    pane["last_seen"] = status
    pane["status_since"] = now
    pane["status_started_at"] = observed_at
    pane["last_status_duration_seconds"] = duration
    pane["generation"] = int(pane.get("generation") or 0) + 1
    if status == "working":
        pane["working_epoch"] = int(pane.get("working_epoch") or 0) + 1
        pane["last_work_started"] = now
        pane["last_work_ended"] = None
    elif previous == "working":
        started = pane.get("last_work_started")
        pane["last_work_ended"] = now
        if started is not None:
            recent = list(pane.get("recent_work") or [])
            recent.append([float(started), now])
            pane["recent_work"] = recent[-MAX_RECENT_WORK:]
            if now - float(started) >= min_work_seconds:
                pane["real_work_epoch"] = int(pane.get("real_work_epoch") or 0) + 1
    if status in IDLE_LIKE:
        if previous not in IDLE_LIKE or pane.get("idle_since") is None:
            pane["idle_since"] = now
    else:
        pane["idle_since"] = None
    return True


def build_payload(
    task: dict[str, Any],
    pane: dict[str, Any],
    *,
    status: str,
    reason: str = "status",
    event_id: str | None = None,
    now: float | None = None,
    coalesced: int = 0,
    hint: dict[str, str] | None = None,
    wait: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the webhook payload from the CURRENT task.  Every key is non-null."""
    now = time.time() if now is None else now
    since = pane.get("status_since")
    phase = str(task.get("phase") or "unknown")
    duration = pane.get("last_status_duration_seconds")
    wait = wait or NO_WAIT
    hint = hint or NO_HINT
    return {
        "event_type": EVENT_TYPE,
        "event_id": event_id or f"{task['id']}:{int(pane.get('generation') or 0)}:{status}",
        "observed_at": iso_time(now),
        "status": status,
        "reason": reason,
        "status_age_seconds": int(max(0.0, now - float(since))) if since is not None else 0,
        "coalesced": int(coalesced or 0),
        "hint": {"kind": str(hint.get("kind") or "none"), "label": str(hint.get("label") or "")},
        "wait": {"reason": str(wait.get("reason") or "none"), "until": str(wait.get("until") or "")},
        "task": task,
        "task_fingerprint": task_fingerprint(task),
        "evidence": evidence_status(task),
        "telemetry": {
            "phase": phase,
            "phase_started_at": str(pane.get("phase_started_at") or "") if pane.get("phase") == phase else "",
            "last_status": str(pane.get("prev_status") or ""),
            "last_status_duration_seconds": float(duration) if duration is not None else 0.0,
            "phase_durations": list(pane.get("phase_durations") or []),
        },
    }


def transition(
    state: dict[str, Any],
    task: dict[str, Any],
    status: str,
    *,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Record a status change and return an immediate wake payload (legacy API).

    The v2 ``Bridge`` does not deliver these payloads directly; it debounces
    and rebuilds the payload at delivery time.
    """
    if status not in STATUS_ALL:
        return None
    pane = state.setdefault("panes", {}).setdefault(task["id"], {"last_seen": None, "generation": 0})
    now_ts = (parse_time(now) if now else None) or time.time()
    if not record_status(pane, task, status, now_ts):
        return None
    if status not in STATUS_WAKE:
        return None
    return build_payload(task, pane, status=status, now=now_ts)


def enqueue(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    event_id = payload["event_id"]
    if any(item.get("event_id") == event_id for item in state["pending"]):
        return False
    state["pending"].append(payload)
    state["pending"] = state["pending"][-MAX_PENDING:]
    return True


# Hint rules: (kind, fixed label, regex).  Earlier rules win ties on one line.
_HINT_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (kind, label, re.compile(pattern, re.IGNORECASE))
    for kind, label, pattern in (
        ("login_required", "/login", r"(?<![\w/.])/login\b"),
        ("login_required", "OAuth token expired", r"oauth token (?:has )?expired"),
        ("login_required", "Invalid API key", r"invalid api key"),
        ("login_required", "authentication_error", r"authentication_error"),
        ("rate_limit", "rate limit", r"rate[ _-]?limit"),
        ("rate_limit", "HTTP 429", r"\b429\b"),
        ("rate_limit", "usage limit", r"usage limit"),
        ("context_limit", "Prompt is too long", r"prompt is too long"),
        ("context_limit", "context limit", r"context (?:window )?limit"),
        ("connection_error", "Connection lost", r"connection lost"),
        ("connection_error", "Connection error", r"connection error"),
        ("connection_error", "API Error", r"api error"),
        ("connection_error", "Request timed out", r"request timed out"),
        ("connection_error", "overloaded", r"overloaded"),
        ("connection_error", "ECONNRESET", r"econnreset"),
        ("connection_error", "socket hang up", r"socket hang up"),
        ("connection_error", "fetch failed", r"fetch failed"),
        ("connection_error", "HTTP 5xx",
         r"\b(?:http|status|error|code)\W{0,3}5\d\d\b"
         r"|\b(?:internal server error|bad gateway|service unavailable|gateway time-?out)\b"),
        ("menu_open", "numbered selector", r"❯\s*\d+\."),
        ("menu_open", "Enter to select", r"enter to select"),
        ("menu_open", "Esc to cancel", r"esc to cancel"),
        ("menu_open", "Do you want to proceed", r"do you want to proceed"),
    )
)


def classify_hint(text: str, recent_lines: int = 15) -> dict[str, str]:
    """Classify pane text into a fixed label; the text itself is never returned."""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    for window in (lines[-recent_lines:], lines):
        best: tuple[tuple[int, int], str, str] | None = None
        for index, line in enumerate(window):
            for priority, (kind, label, pattern) in enumerate(_HINT_RULES):
                if pattern.search(line):
                    key = (index, -priority)
                    if best is None or key > best[0]:
                        best = (key, kind, label)
                    break
        if best is not None:
            return {"kind": best[1], "label": best[2][:40]}
    return dict(NO_HINT)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class DeliveryResult(NamedTuple):
    outcome: str  # accepted | ignored | error
    delivery_id: str = ""
    detail: str = ""


def send_owner_alert(env_path: str, chat_id: str, text: str, timeout: float = 15.0) -> bool:
    """Message the owner through the profile's Telegram bot directly (not through Hermes,
    which may be the thing that is broken). The token is read from the profile's .env and never logged."""
    import urllib.parse
    import urllib.request
    token = ""
    try:
        for line in Path(env_path).read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "TELEGRAM_BOT_TOKEN":
                token = value.strip().strip('"').strip("'")
    except OSError as exc:
        log(f"alert skipped: cannot read bot token ({type(exc).__name__})")
        return False
    if not token or not chat_id:
        log("alert skipped: no bot token or chat")
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                    timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 - an alert must never take the bridge down
        log(f"alert failed: {type(exc).__name__}")
        return False


def deliver_payload(hermes_bin: str, webhook: str, payload: dict[str, Any], timeout: int) -> DeliveryResult:
    """Run ``hermes webhook test`` once with a bounded timeout and classify the reply."""
    command = [hermes_bin, "webhook", "test", webhook, "--payload", json.dumps(payload, ensure_ascii=False)]
    try:
        proc = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, min(int(timeout), MAX_WEBHOOK_TIMEOUT)),
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DeliveryResult("error", "", type(exc).__name__)
    output = (proc.stdout or "").strip().replace("\n", " ")[-500:]
    if proc.returncode != 0:
        return DeliveryResult("error", "", output)
    # ``hermes webhook test`` exits zero even when the route returns HTTP 200
    # with {"status":"ignored"}; only an accepted agent run counts as delivery.
    match = re.search(r"Response \((\d+)\):\s*(\{.*\})\s*$", output)
    if not match:
        return DeliveryResult("error", "", f"unparseable webhook response: {output}")
    http_status = int(match.group(1))
    if not 200 <= http_status < 300:
        return DeliveryResult("error", "", f"http_status={http_status}")
    try:
        response = json.loads(match.group(2))
    except json.JSONDecodeError:
        return DeliveryResult("error", "", f"invalid webhook response: {output}")
    status = response.get("status") if isinstance(response, dict) else None
    delivery_id = str(response.get("delivery_id") or "") if isinstance(response, dict) else ""
    if status in {"accepted", "delivered", "coalesced"}:
        return DeliveryResult("accepted", delivery_id, f"status={status}")
    if status == "ignored":
        return DeliveryResult("ignored", "", f"status=ignored reason={response.get('reason', '-')}")
    return DeliveryResult("error", "", f"status={status}")


def deliver_one(hermes_bin: str, webhook: str, payload: dict[str, Any], timeout: int) -> tuple[bool, str]:
    """Legacy boolean wrapper around ``deliver_payload``."""
    result = deliver_payload(hermes_bin, webhook, payload, timeout)
    return result.outcome == "accepted", result.detail


def pending_is_current(
    payload: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    live_pane_ids: set[str],
) -> bool:
    """Return whether a payload still matches the exact current task revision."""
    payload_task = payload.get("task")
    if not isinstance(payload_task, dict):
        return False
    task_id = str(payload_task.get("id") or "")
    current = tasks.get(task_id)
    if current is None:
        return False
    pane_id = str(current.get("pane_id") or "")
    if not pane_id or pane_id not in live_pane_ids:
        return False
    expected = str(payload.get("task_fingerprint") or "")
    return bool(expected) and expected == task_fingerprint(current)


def flush_pending(
    state: dict[str, Any],
    state_path: Path,
    hermes_bin: str,
    webhook: str,
    timeout: int,
    *,
    tasks: dict[str, dict[str, Any]],
    live_pane_ids: set[str],
) -> bool:
    """Deliver the oldest legacy ``pending`` payload once (v2 semantics).

    The payload is rebuilt from the current task when its fingerprint changed;
    it is dropped only if the task is gone/disabled or its pane is not live.
    ``ignored`` is final.  Returns False only on a transport failure.
    """
    if not state["pending"]:
        return True
    payload = state["pending"][0]
    payload_task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    current = tasks.get(str(payload_task.get("id") or ""))
    if current is None or current.get("pane_id") not in live_pane_ids:
        state["pending"].pop(0)
        atomic_json(state_path, state)
        log(f"dropped event={payload.get('event_id')} reason=gone")
        return True
    if payload.get("task_fingerprint") != task_fingerprint(current):
        payload = dict(payload, task=current, task_fingerprint=task_fingerprint(current),
                       evidence=evidence_status(current))
    result = deliver_payload(hermes_bin, webhook, payload, timeout)
    if result.outcome == "error":
        log(f"webhook delivery failed event={payload.get('event_id')} detail={result.detail}")
        return False
    state["pending"].pop(0)
    atomic_json(state_path, state)
    if result.outcome == "accepted":
        log(f"delivered event={payload.get('event_id')} delivery_id={result.delivery_id or '-'}")
    else:
        log(f"dropped event={payload.get('event_id')} reason=ignored")
    return True


def event_status(message: dict[str, Any]) -> tuple[str | None, str | None]:
    if message.get("event") != "pane.agent_status_changed":
        return None, None
    data = message.get("data")
    if not isinstance(data, dict):
        return None, None
    pane_id = data.get("pane_id")
    status = data.get("agent_status")
    return (str(pane_id), str(status)) if pane_id and status else (None, None)


def lookup_controller_sessions(
    db_path: Path, chat_ids: list[str], since: float
) -> dict[str, tuple[float | None, float | None]] | None:
    """Read-only lookup of controller runs in the gateway ``state.db``.

    Returns {chat_id: (started_at, ended_at)} (ended_at None while any row of
    that chat is still open), or None when the database cannot be read.
    """
    if not chat_ids:
        return {}
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            placeholders = ",".join("?" * len(chat_ids))
            rows = conn.execute(
                f"SELECT chat_id, started_at, ended_at FROM sessions "
                f"WHERE started_at >= ? AND chat_id IN ({placeholders})",
                [since, *chat_ids],
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return None
    found: dict[str, dict[str, Any]] = {}
    for chat_id, started, ended in rows:
        item = found.setdefault(chat_id, {"started": [], "ended": [], "open": False})
        if started is not None:
            item["started"].append(float(started))
        if ended is None:
            item["open"] = True
        else:
            item["ended"].append(float(ended))
    return {
        chat_id: (
            min(item["started"]) if item["started"] else None,
            None if item["open"] or not item["ended"] else max(item["ended"]),
        )
        for chat_id, item in found.items()
    }


# ---------------------------------------------------------------------------
# Bridge v2 core
# ---------------------------------------------------------------------------


class BridgeConfig:
    """Paths and tunables (plain class: the module is also loaded without sys.modules)."""

    DEFAULTS: dict[str, Any] = {
        "socket": default_socket(),
        "webhook": "herdr-agent-events",
        "hermes_bin": shutil.which("hermes") or "hermes",
        "herdr_bin": shutil.which("herdr") or "herdr",
        "session": "default",
        "project": "",
        "profile": "",
        "cwd_prefix": (),
        "sibling_prefix": False,
        "webhook_timeout": MAX_WEBHOOK_TIMEOUT,
        "loop_interval": 1.0,
        "debounce_seconds": 20.0,
        "blocked_debounce_seconds": 3.0,
        "unknown_grace_seconds": 45.0,
        "hold_seconds": 900.0,
        "stall_seconds": 900.0,
        "working_no_output_seconds": 1200.0,
        "watchdog": True,
        "watchdog_interval": 60.0,
        "revision_poll_seconds": 300.0,
        "min_work_seconds": 20.0,
        "db_poll_seconds": 5.0,
        "inflight_no_row_seconds": 180.0,
        "inflight_max_seconds": 1800.0,
        "alert_chat": "",
        "alert_env": "",
        "alert_transport_interval": 3600.0,
        "stall_alert_seconds": 5400.0,
        "topology_min_interval": 5.0,
        "pane_refresh_seconds": 60.0,
    }

    def __init__(self, registry: Path | str, state: Path | str, state_db: Path | str, **options: Any) -> None:
        unknown = set(options) - set(self.DEFAULTS)
        if unknown:
            raise TypeError(f"unknown BridgeConfig options: {sorted(unknown)}")
        for key, default in self.DEFAULTS.items():
            setattr(self, key, options.get(key, default))
        self.registry = Path(registry)
        self.state = Path(state)
        self.state_db = Path(state_db)
        self.cwd_prefix = list(self.cwd_prefix or [])
        self.webhook_timeout = max(1, min(int(self.webhook_timeout), MAX_WEBHOOK_TIMEOUT))
        self.loop_interval = max(0.05, min(float(self.loop_interval), 1.0))

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "BridgeConfig":
        state_db = args.state_db
        if not state_db:
            home = os.environ.get("HERMES_HOME", "").strip()
            state_db = str(Path(home) / "state.db") if home else str(Path(args.registry).parent.parent / "state.db")
        return cls(
            registry=Path(args.registry),
            state=Path(args.state),
            state_db=Path(state_db),
            socket=args.socket,
            webhook=args.webhook,
            hermes_bin=args.hermes_bin,
            herdr_bin=args.herdr_bin,
            session=args.session,
            project=args.project,
            profile=getattr(args, "profile", "") or "",
            alert_chat=getattr(args, "alert_chat", "") or "",
            alert_env=getattr(args, "alert_env", "") or "",
            cwd_prefix=list(args.cwd_prefix or []),
            sibling_prefix=bool(args.sibling_prefix),
            webhook_timeout=args.webhook_timeout,
            loop_interval=args.registry_poll,
            debounce_seconds=args.debounce_seconds,
            blocked_debounce_seconds=args.blocked_debounce_seconds,
            unknown_grace_seconds=args.unknown_grace_seconds,
            hold_seconds=args.hold_seconds,
            stall_seconds=args.stall_seconds,
            working_no_output_seconds=args.working_no_output_seconds,
            watchdog=not args.no_watchdog,
            watchdog_interval=args.watchdog_interval,
        )


class _Job:
    """One delivery attempt, run in a daemon thread (or inline for tests)."""

    def __init__(self, fn: Callable[..., Any], *args: Any, threaded: bool) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self._done = threading.Event()
        if threaded:
            threading.Thread(target=self._run, args=(fn, args), daemon=True,
                             name="herdr-bridge-delivery").start()
        else:
            self._run(fn, args)

    def _run(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
        try:
            self.result = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - reported to the main loop
            self.error = exc
        finally:
            self._done.set()

    def done(self) -> bool:
        return self._done.is_set()


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def _short(value: Any, limit: int = 200) -> str:
    return str(value).replace("\n", " ")[:limit]


class Bridge:
    """Single-threaded state machine; only the delivery attempt runs in a worker.

    All state mutation happens on the caller's thread through ``handle_message``
    and ``tick``.  Tests drive it with explicit ``now`` values and fakes.
    """

    def __init__(
        self,
        cfg: BridgeConfig,
        *,
        list_panes: Callable[[], list[dict[str, Any]]] | None = None,
        read_pane: Callable[[str], str] | None = None,
        agent_get: Callable[[str], dict[str, Any]] | None = None,
        deliver: Callable[[dict[str, Any]], DeliveryResult] | None = None,
        threaded: bool = True,
        clock: Callable[[], float] = time.time,
        alert: Callable[[str], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        if alert is None and cfg.alert_chat and cfg.alert_env:
            def alert(text: str) -> None:  # off the event loop: Telegram may be slow
                threading.Thread(target=send_owner_alert, args=(cfg.alert_env, cfg.alert_chat, text),
                                 daemon=True).start()
        self._alert_sink = alert or (lambda text: None)
        self._list_panes = list_panes or (lambda: list_herdr_panes(cfg.herdr_bin, cfg.session))
        self._read_pane = read_pane or (lambda pane_id: read_pane_tail(cfg.herdr_bin, cfg.session, pane_id))
        self._agent_get = agent_get or (lambda pane_id: herdr_agent_info(cfg.herdr_bin, cfg.session, pane_id))
        self._deliver = deliver or (
            lambda payload: deliver_payload(cfg.hermes_bin, cfg.webhook, payload, cfg.webhook_timeout)
        )
        self.threaded = threaded
        self.registered: dict[str, dict[str, Any]] = {}
        self.extras: dict[str, dict[str, Any]] = {}
        self.tombstoned: set[str] = set()
        self.registry_sig: Any = ("never-loaded",)
        self.panes: list[dict[str, Any]] = []
        self.panes_loaded = False
        self.tasks: dict[str, dict[str, Any]] = {}
        self.by_pane: dict[str, dict[str, Any]] = {}
        self.live: set[str] = set()
        self.subscribed: set[str] | None = None
        self.need_resubscribe = False
        self.topology_dirty = False
        self.last_pane_refresh = float("-inf")
        self.last_watchdog: float | None = None
        self.last_db_check = float("-inf")
        self.sending: dict[str, Any] | None = None
        self.dirty = False
        self.state = self._load_state(clock())

    # -- state -------------------------------------------------------------

    def _load_state(self, now: float) -> dict[str, Any]:
        try:
            state = load_state(self.cfg.state)
        except (OSError, ValueError) as exc:
            log(f"ERROR state load failed; starting fresh error={type(exc).__name__}: {_short(exc)}")
            try:
                os.replace(self.cfg.state, self.cfg.state.with_name(self.cfg.state.name + ".corrupt"))
            except OSError:
                pass
            state = {"version": 1, "panes": {}, "pending": [], "queue": [], "workflows": {}}
        panes = state["panes"]
        for task_id in list(panes):
            pane = panes[task_id]
            if not isinstance(pane, dict):
                pane = panes[task_id] = {"last_seen": None, "generation": 0}
            pane.setdefault("last_seen", None)
            pane.setdefault("generation", 0)
            pane.setdefault("working_epoch", 0)
            # v1 state has no epoch timestamps: start watchdog clocks now.
            if pane.get("last_seen") and pane.get("status_since") is None:
                pane["status_since"] = now
            if pane.get("last_seen") in IDLE_LIKE and pane.get("idle_since") is None:
                pane["idle_since"] = pane["status_since"]
        state["queue"] = [
            entry for entry in state["queue"]
            if isinstance(entry, dict) and isinstance(entry.get("task_id"), str)
            and entry.get("status") in STATUS_ALL and entry.get("reason") in WAKE_REASONS
            and isinstance(entry.get("event_id"), str)
        ]
        state["workflows"] = {k: v for k, v in state["workflows"].items() if isinstance(v, dict)}
        legacy, state["pending"] = state["pending"], []
        self.state = state
        for payload in legacy:
            task = payload.get("task") if isinstance(payload, dict) else None
            status = payload.get("status") if isinstance(payload, dict) else None
            if not isinstance(task, dict) or status not in STATUS_WAKE or not task.get("id"):
                continue
            self._enqueue({
                "task_id": str(task["id"]), "status": status, "reason": "status",
                "event_id": str(payload.get("event_id") or f"{task['id']}:0:{status}"),
                "queued_at": now, "next_at": now, "coalesced": 0, "attempts": 0, "rebuilt": False,
            })
        if legacy:
            log(f"migrated legacy pending events={len(legacy)} queued={len(state['queue'])}")
            self.dirty = True
        return state

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            atomic_json(self.cfg.state, self.state)
            self.dirty = False
        except OSError as exc:
            log(f"ERROR state save failed error={type(exc).__name__}: {_short(exc)}")

    def _pane(self, task_id: str) -> dict[str, Any]:
        return self.state["panes"].setdefault(
            task_id, {"last_seen": None, "generation": 0, "working_epoch": 0})

    def workflow_key(self, task_id: str) -> str:
        """Author and linked reviewer share one single-flight key."""
        return self.extras.get(task_id, {}).get("parent_task_id") or task_id

    def inflight(self, workflow: str) -> dict[str, Any] | None:
        entry = self.state["workflows"].get(workflow)
        return entry.get("inflight") if isinstance(entry, dict) else None

    def wait_for(self, task_id: str) -> dict[str, str]:
        return dict(self.extras.get(task_id, {}).get("wait") or NO_WAIT)

    # -- registry / topology ------------------------------------------------

    def reload_registry(self, now: float, *, force: bool = False) -> bool:
        """Reload on file change; on any error keep the last good registry."""
        signature = _file_signature(self.cfg.registry)
        if not force and signature == self.registry_sig:
            return False
        self.registry_sig = signature
        try:
            raw = load_json(self.cfg.registry, {"version": 1, "tasks": []})
            registered = normalize_registry(raw)
            extras = registry_extras(raw)
            tombstoned = tombstones_from_raw(raw)
        except (OSError, ValueError) as exc:
            log(f"ERROR registry load failed; keeping last good tasks={len(self.registered)} "
                f"error={type(exc).__name__}: {_short(exc)}")
            return False
        changed = (registered, extras, tombstoned) != (self.registered, self.extras, self.tombstoned)
        self.registered, self.extras, self.tombstoned = registered, extras, tombstoned
        self._rebuild_tasks()
        if any(self.state["panes"].get(tid, {}).get("last_seen") is None for tid in self.tasks):
            self.topology_dirty = True  # seed a baseline status for new tasks
        return changed

    def refresh_panes(self, now: float) -> bool:
        self.last_pane_refresh = now
        self.topology_dirty = False
        if not self.cfg.cwd_prefix:
            return True
        try:
            panes = self._list_panes()
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            log(f"pane discovery failed reason={type(exc).__name__}:{_short(exc)}")
            return False
        self.panes = panes
        self.panes_loaded = True
        self._rebuild_tasks()
        self._seed_statuses(now)
        return True

    def _rebuild_tasks(self) -> None:
        if self.cfg.cwd_prefix:
            tasks = merge_discovered_panes(
                self.registered,
                self.panes,
                project=self.cfg.project,
                session=self.cfg.session,
                cwd_prefixes=self.cfg.cwd_prefix,
                sibling_prefix=self.cfg.sibling_prefix,
                tombstoned_panes=self.tombstoned,
            )
            live = {str(pane.get("pane_id") or "") for pane in self.panes} - {""}
        else:
            tasks = dict(self.registered)
            live = {task["pane_id"] for task in tasks.values()}
        self.tasks = tasks
        self.by_pane = {task["pane_id"]: task for task in tasks.values()}
        self.live = live
        if self.subscribed is not None and set(self.by_pane) != self.subscribed and not self.need_resubscribe:
            log(f"routed pane set changed; resubscribing panes={','.join(sorted(self.by_pane)) or '-'}")
            self.need_resubscribe = True

    def _seed_statuses(self, now: float) -> None:
        """Baseline (no wake) for routed tasks whose status was never observed."""
        for pane_info in self.panes:
            task = self.by_pane.get(str(pane_info.get("pane_id") or ""))
            status = str(pane_info.get("agent_status") or "")
            if task and status in STATUS_ALL and self._pane(task["id"]).get("last_seen") is None:
                self.observe(task, status, now, source="snapshot")

    def prepare_subscription(self, now: float) -> bool:
        """Reload registry and take a fresh pane snapshot right before subscribing."""
        self.reload_registry(now)
        return self.refresh_panes(now)

    def mark_subscribed(self, now: float) -> None:
        """Record the subscribed pane set and reconcile statuses missed while offline."""
        self.subscribed = set(self.by_pane)
        self.need_resubscribe = False
        for pane_info in self.panes:
            task = self.by_pane.get(str(pane_info.get("pane_id") or ""))
            status = str(pane_info.get("agent_status") or "")
            if task and status in STATUS_ALL:
                self.observe(task, status, now, source="snapshot")
        log(f"subscribed project={self.cfg.project or 'registry'} "
            f"cwd_prefixes={','.join(self.cfg.cwd_prefix) or '-'} "
            f"panes={','.join(sorted(self.by_pane)) or '-'}")

    # -- events -------------------------------------------------------------

    def handle_line(self, line: bytes, now: float) -> None:
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            log("ignored malformed Herdr event")
            return
        if isinstance(message, dict):
            self.handle_message(message, now)

    def handle_message(self, message: dict[str, Any], now: float) -> None:
        if is_topology_event(message):
            self.topology_dirty = True
            return
        pane_id, status = event_status(message)
        task = self.by_pane.get(pane_id or "")
        if task and status in STATUS_ALL:
            self.observe(task, status, now)

    def observe(self, task: dict[str, Any], status: str, now: float, *, source: str = "event") -> None:
        task_id = task["id"]
        pane = self._pane(task_id)
        previous = pane.get("last_seen")
        if status not in STATUS_ALL or previous == status:
            return
        record_status(pane, task, status, now, min_work_seconds=self.cfg.min_work_seconds)
        if status == "working":
            # A snapshot only tells us the pane is working now, not when the work began.
            pane["work_start_observed"] = source == "event"
        self.dirty = True
        suffix = "" if source == "event" else f" source={source}"
        if source == "snapshot" and previous is None:
            log(f"status task={task_id} pane={task['pane_id']} status={status}{suffix} baseline")
            return
        decision = self._schedule(task, pane, status, now)
        log(f"status task={task_id} pane={task['pane_id']} status={status}{suffix} {decision}")

    def _schedule(self, task: dict[str, Any], pane: dict[str, Any], status: str, now: float) -> str:
        task_id = task["id"]
        if status == "working":
            had_wake = bool(pane.get("wake"))
            pane["wake"] = None
            dropped = self._drop_queued(task_id, "working", lambda entry: entry["status"] != "working")
            return "cancelled" if had_wake or dropped else "tracking"
        if task_id.startswith("auto:"):
            # Unregistered panes have no change/policy and the route script always drops them,
            # so a wake would only cost a delivery. Track status, never wake.
            pane["wake"] = None
            return f"no-wake(auto {status})"
        delay = {
            "blocked": self.cfg.blocked_debounce_seconds,
            "unknown": self.cfg.unknown_grace_seconds,
        }.get(status, self.cfg.debounce_seconds)
        pane["wake"] = {"status": status, "due": now + delay}
        return f"scheduled in={delay:g}s"

    def _drop_queued(self, task_id: str, reason: str, predicate: Callable[[dict[str, Any]], bool]) -> bool:
        queue = self.state["queue"]
        dropped = [e for e in queue if e["task_id"] == task_id and predicate(e)]
        if not dropped:
            return False
        for entry in dropped:
            log(f"dropped task={task_id} event={entry['event_id']} reason={reason}")
        self.state["queue"] = [e for e in queue if not any(e is d for d in dropped)]
        self.dirty = True
        return True

    # -- wake requests --------------------------------------------------------

    def _held(self, pane: dict[str, Any], status: str, now: float) -> bool:
        last = pane.get("last_delivery")
        return bool(
            isinstance(last, dict)
            and last.get("working_epoch") == int(pane.get("working_epoch") or 0)
            and last.get("status") == status
            and now - float(last.get("at") or 0) < self.cfg.hold_seconds
        )

    def request_wake(self, task: dict[str, Any], status: str, reason: str, now: float, *,
                     coalesced: int = 0) -> None:
        task_id = task["id"]
        workflow = self.workflow_key(task_id)
        inflight = self.inflight(workflow)
        if inflight:
            self._defer(task_id, status, reason, now, inflight, workflow)
            return
        pane = self._pane(task_id)
        generation = int(pane.get("generation") or 0)
        if reason == "status":
            event_id = f"{task_id}:{generation}:{status}"
        else:
            sequence = pane.setdefault("reason_seq", {})
            sequence[reason] = int(sequence.get(reason) or 0) + 1
            event_id = f"{task_id}:{generation}:{status}:{reason}{sequence[reason]}"
        self._enqueue({
            "task_id": task_id, "status": status, "reason": reason, "event_id": event_id,
            "queued_at": now, "next_at": now, "coalesced": coalesced, "attempts": 0, "rebuilt": False,
        })

    def _enqueue(self, entry: dict[str, Any]) -> None:
        queue = self.state["queue"]
        self.dirty = True
        for index, old in enumerate(queue):
            if old["task_id"] == entry["task_id"]:
                entry["coalesced"] = int(old.get("coalesced") or 0) + 1 + int(entry.get("coalesced") or 0)
                entry["attempts"] = int(old.get("attempts") or 0)
                entry["next_at"] = max(float(entry["next_at"]), float(old.get("next_at") or 0))
                entry["queued_at"] = old.get("queued_at", entry["queued_at"])
                queue[index] = entry
                log(f"coalesced task={entry['task_id']} event={entry['event_id']} "
                    f"replaces={old['event_id']} coalesced={entry['coalesced']}")
                return
        queue.append(entry)

    def _defer(self, task_id: str, status: str, reason: str, now: float,
               inflight: dict[str, Any], workflow: str) -> None:
        pane = self._pane(task_id)
        since = float(inflight.get("since") or now)
        deferred = pane.get("deferred") or {"count": 0, "first_at": now, "ref": since}
        deferred["count"] = int(deferred.get("count") or 0) + 1
        deferred["status"] = status
        deferred["reason"] = reason
        deferred["last_at"] = now
        deferred["ref"] = min(float(deferred.get("ref") or since), since)
        pane["deferred"] = deferred
        self.dirty = True
        log(f"deferred task={task_id} workflow={workflow} status={status} reason={reason} "
            f"count={deferred['count']} inflight={inflight.get('event_id')}")

    def _real_work_after(self, pane: dict[str, Any], ref: float) -> bool:
        for started, ended in pane.get("recent_work") or []:
            if float(ended) >= ref and float(ended) - float(started) >= self.cfg.min_work_seconds:
                return True
        return False

    def _workflow_busy(self, workflow: str) -> bool:
        if self.inflight(workflow):
            return True
        if self.sending and self.workflow_key(self.sending["entry"]["task_id"]) == workflow:
            return True
        return any(self.workflow_key(entry["task_id"]) == workflow for entry in self.state["queue"])

    # -- tick -----------------------------------------------------------------

    def tick(self, now: float) -> None:
        """Run timers and at most one delivery attempt; never blocks on delivery."""
        self.reload_registry(now)
        if self.cfg.cwd_prefix and now - self.last_pane_refresh >= self.cfg.pane_refresh_seconds:
            self.topology_dirty = True
        if self.topology_dirty and now - self.last_pane_refresh >= self.cfg.topology_min_interval:
            self.refresh_panes(now)
        self._collect(now)
        self._check_inflight(now)
        self._process_due_wakes(now)
        self._watchdog(now)
        self._release_deferred(now)
        self._dispatch(now)
        self.save()

    def safe_tick(self, now: float) -> None:
        try:
            self.tick(now)
        except Exception as exc:  # noqa: BLE001 - keep reading the socket
            log(f"ERROR tick failed {type(exc).__name__}: {_short(exc)} "
                f"trace={_short(traceback.format_exc(limit=3), 600)}")

    def _process_due_wakes(self, now: float) -> None:
        for task_id, pane in list(self.state["panes"].items()):
            wake = pane.get("wake")
            if not isinstance(wake, dict) or float(wake.get("due") or 0) > now:
                continue
            pane["wake"] = None
            self.dirty = True
            status = wake.get("status")
            task = self.tasks.get(task_id)
            if task is None or task["pane_id"] not in self.live:
                log(f"dropped task={task_id} status={status} reason=gone")
                continue
            if pane.get("last_seen") != status:
                continue
            if status != "blocked" and self._held(pane, status, now):
                last = pane["last_delivery"]
                log(f"held task={task_id} status={status} last_event={last.get('event_id')} "
                    f"age={int(now - float(last.get('at') or now))}s")
                continue
            if status in ("idle", "done") and self._quiet_wait(task_id, pane, now):
                log(f"held task={task_id} status={status} reason=active_wait_short_turn "
                    f"until={self.wait_for(task_id).get('until')}")
                continue
            self.request_wake(task, status, "status", now)

    def _quiet_wait(self, task_id: str, pane: dict[str, Any], now: float) -> bool:
        """A controller declared a quiet "wake me at `until`" and the agent only made a short turn.

        Long-running jobs make the agent post brief progress turns; waking a controller
        for each costs a full LLM run and changes nothing. Only a wait with `quiet: true`
        opts in: a short turn can also be a real result (e.g. a background test run just
        finished), so a plain wait never holds. The wait's own deadline, a blocked status,
        and any turn longer than SHORT_TURN_SECONDS still wake it.
        """
        wait = self.wait_for(task_id)
        if wait.get("quiet") is not True or wait.get("reason") not in ("scheduled", "external"):
            return False
        until = parse_time(wait.get("until"))
        if until is None or until <= now:
            return False
        started, ended = pane.get("last_work_started"), pane.get("last_work_ended")
        if not started or not ended or pane.get("work_start_observed") is not True:
            return False
        return float(ended) - float(started) < SHORT_TURN_SECONDS

    def _release_deferred(self, now: float) -> None:
        groups: dict[str, list[str]] = {}
        for task_id, pane in self.state["panes"].items():
            if pane.get("deferred"):
                groups.setdefault(self.workflow_key(task_id), []).append(task_id)
        for workflow, task_ids in groups.items():
            if self._workflow_busy(workflow):
                continue
            qualified: list[tuple[int, float, str]] = []
            for task_id in task_ids:
                pane = self._pane(task_id)
                deferred = pane["deferred"]
                task = self.tasks.get(task_id)
                status = pane.get("last_seen")
                verdict = None
                if task is None or task["pane_id"] not in self.live:
                    verdict = "gone"
                elif status == "working":
                    verdict = "working_now"
                elif status == "blocked":
                    qualified.append((0, float(deferred.get("first_at") or now), task_id))
                    continue
                elif status in IDLE_LIKE and not (status == "unknown" and task_id.startswith("auto:")) \
                        and self._real_work_after(pane, float(deferred.get("ref") or now)):
                    qualified.append((1, float(deferred.get("first_at") or now), task_id))
                    continue
                else:
                    verdict = "self_caused"
                pane["deferred"] = None
                self.dirty = True
                log(f"dropped task={task_id} deferred={deferred.get('count')} reason={verdict} status={status}")
            if not qualified:
                continue
            _, _, task_id = min(qualified)
            pane = self._pane(task_id)
            deferred, pane["deferred"] = pane["deferred"], None
            log(f"released deferred task={task_id} workflow={workflow} status={pane.get('last_seen')} "
                f"count={deferred.get('count')} waiting={len(qualified) - 1}")
            self.request_wake(self.tasks[task_id], pane["last_seen"], "deferred", now,
                              coalesced=max(0, int(deferred.get("count") or 1) - 1))

    # -- delivery ---------------------------------------------------------------

    def _dispatch(self, now: float) -> None:
        if self.sending is not None:
            return
        queue = self.state["queue"]
        index = 0
        while index < len(queue):
            entry = queue[index]
            if float(entry.get("next_at") or 0) > now:
                index += 1
                continue
            queue.pop(index)
            self.dirty = True
            task_id = entry["task_id"]
            task = self.tasks.get(task_id)
            if task is None or task["pane_id"] not in self.live:
                log(f"dropped task={task_id} event={entry['event_id']} reason=gone")
                continue
            workflow = self.workflow_key(task_id)
            inflight = self.inflight(workflow)
            if inflight:
                self._defer(task_id, entry["status"], entry["reason"], now, inflight, workflow)
                continue
            pane = self._pane(task_id)
            if pane.get("last_seen") != entry["status"]:
                log(f"dropped task={task_id} event={entry['event_id']} reason=superseded "
                    f"current={pane.get('last_seen')}")
                continue
            self._start_delivery(entry, task, pane, now)
            return

    def _start_delivery(self, entry: dict[str, Any], task: dict[str, Any], pane: dict[str, Any],
                        now: float) -> None:
        payload = build_payload(
            task, pane, status=entry["status"], reason=entry["reason"], event_id=entry["event_id"],
            now=now, coalesced=int(entry.get("coalesced") or 0), wait=self.wait_for(task["id"]),
        )
        wire = json.loads(json.dumps(payload, ensure_ascii=False))  # detached copy for the worker
        self.sending = {"entry": entry, "fingerprint": payload["task_fingerprint"], "started": now}
        self.sending["job"] = _Job(self._delivery_job, wire, task["pane_id"], threaded=self.threaded)
        if not self.threaded:
            self._collect(now)

    def _delivery_job(self, payload: dict[str, Any], pane_id: str) -> tuple[DeliveryResult, dict[str, str]]:
        try:
            hint = classify_hint(self._read_pane(pane_id))
        except Exception:  # noqa: BLE001 - hint is best effort
            hint = dict(NO_HINT)
        payload["hint"] = hint
        return self._deliver(payload), hint

    def _collect(self, now: float) -> None:
        sending = self.sending
        if sending is None:
            return
        if not sending["job"].done():
            # Subprocess timeouts bound a worker to ~50 s; never wedge the queue.
            if now - float(sending["started"]) > MAX_WEBHOOK_TIMEOUT + 30:
                self.sending = None
                self._on_transport_error(sending["entry"], DeliveryResult("error", "", "worker stuck"), now)
            return
        self.sending = None
        self.dirty = True
        job = sending["job"]
        if job.error is not None or not isinstance(job.result, tuple):
            result, hint = DeliveryResult("error", "", f"worker {type(job.error).__name__}"), dict(NO_HINT)
        else:
            result, hint = job.result
        entry = sending["entry"]
        if result.outcome == "accepted":
            self._on_accepted(entry, result, hint, now)
        elif result.outcome == "ignored":
            self._on_ignored(entry, sending["fingerprint"], result, now)
        else:
            self._on_transport_error(entry, result, now)

    def _on_accepted(self, entry: dict[str, Any], result: DeliveryResult, hint: dict[str, str],
                     now: float) -> None:
        task_id = entry["task_id"]
        workflow = self.workflow_key(task_id)
        pane = self._pane(task_id)
        self.state["workflows"].setdefault(workflow, {})["inflight"] = {
            "event_id": entry["event_id"], "delivery_id": result.delivery_id, "since": now,
            "task_id": task_id, "status": entry["status"], "reason": entry["reason"],
            "controller_started_at": None, "controller_ended_at": None,
        }
        pane["last_delivery"] = {
            "at": now, "status": entry["status"], "reason": entry["reason"],
            "event_id": entry["event_id"], "delivery_id": result.delivery_id,
            "working_epoch": int(pane.get("working_epoch") or 0),
        }
        pane["deferred"] = None
        log(f"delivered task={task_id} event={entry['event_id']} reason={entry['reason']} "
            f"workflow={workflow} delivery_id={result.delivery_id or '-'} hint={hint.get('kind')} "
            f"coalesced={entry.get('coalesced', 0)}")

    def _on_ignored(self, entry: dict[str, Any], sent_fingerprint: str, result: DeliveryResult,
                    now: float) -> None:
        task_id = entry["task_id"]
        if not entry.get("rebuilt"):
            self.reload_registry(now, force=True)
            task = self.tasks.get(task_id)
            if task is not None and task["pane_id"] in self.live and task_fingerprint(task) != sent_fingerprint:
                if any(e["task_id"] == task_id for e in self.state["queue"]):
                    log(f"dropped task={task_id} event={entry['event_id']} reason=ignored_newer_queued")
                    return
                self.state["queue"].insert(0, dict(entry, rebuilt=True, next_at=now))
                log(f"retry task={task_id} event={entry['event_id']} reason=ignored_fingerprint_changed")
                return
        log(f"dropped task={task_id} event={entry['event_id']} reason=ignored detail={_short(result.detail)}")

    def _on_transport_error(self, entry: dict[str, Any], result: DeliveryResult, now: float) -> None:
        task_id = entry["task_id"]
        attempts = int(entry.get("attempts") or 0) + 1
        if attempts > len(RETRY_BACKOFF):
            log(f"ERROR dropped task={task_id} event={entry['event_id']} reason=transport "
                f"attempts={attempts} detail={_short(result.detail)}")
            self.alert_owner("transport", f"⚠️ {self.cfg.project}: agent events are not reaching Hermes ({_short(result.detail)}). "
                f"Tasks wait until delivery recovers: bin/harness status {self.cfg.profile}",
                now, self.cfg.alert_transport_interval)
            return
        delay = RETRY_BACKOFF[attempts - 1]
        retry = dict(entry, attempts=attempts, next_at=now + delay)
        for newer in self.state["queue"]:
            if newer["task_id"] == task_id:
                newer["attempts"] = max(int(newer.get("attempts") or 0), attempts)
                newer["next_at"] = max(float(newer.get("next_at") or 0), retry["next_at"])
                newer["coalesced"] = int(newer.get("coalesced") or 0) + 1
                break
        else:
            self.state["queue"].insert(0, retry)
        log(f"retry task={task_id} event={entry['event_id']} reason=transport attempt={attempts} "
            f"in={delay}s detail={_short(result.detail)}")

    # -- controller runs (single-flight) ----------------------------------------------

    def _check_inflight(self, now: float) -> None:
        if now - self.last_db_check < self.cfg.db_poll_seconds:
            return
        self.last_db_check = now
        active = {wf: entry["inflight"] for wf, entry in self.state["workflows"].items()
                  if isinstance(entry.get("inflight"), dict)}
        if not active:
            return
        # Hermes has keyed webhook sessions as webhook:<route>:<id> and, since 0.21.5, as
        # webhook:v2:<b64 [profile, route, id]> (profile "default" on an unbound route).
        chats: dict[str, list[str]] = {}
        for wf, inf in active.items():
            if inf.get("delivery_id"):
                ids = webhook_ids.session_chat_ids(self.cfg.webhook, inf["delivery_id"], self.cfg.profile or None)
                ids.append(webhook_ids.session_chat_ids(self.cfg.webhook, inf["delivery_id"], None)[1])
                chats[wf] = list(dict.fromkeys(ids))
        oldest = min(float(inf.get("since") or now) for inf in active.values())
        rows = lookup_controller_sessions(self.cfg.state_db, sorted({c for ids in chats.values() for c in ids}),
                                          oldest - 600)
        for workflow, inflight in active.items():
            age = now - float(inflight.get("since") or now)
            row = None
            if rows is not None:
                row = next((rows[c] for c in chats.get(workflow, []) if c in rows), None)
            how = None
            ended_at = now
            if row is not None:
                started, ended = row
                if started is not None and inflight.get("controller_started_at") != started:
                    inflight["controller_started_at"] = started
                    self.dirty = True
                if ended is not None:
                    how, ended_at = "db", ended
            elif age >= self.cfg.inflight_no_row_seconds:
                how = "assumed_no_row" if rows is not None else "assumed_db_unreadable"
            if how is None and age >= self.cfg.inflight_max_seconds:
                how = "hard_cap"
                log(f"WARNING controller still open after {int(age)}s workflow={workflow} "
                    f"event={inflight.get('event_id')}; releasing single-flight")
            if how:
                self._end_inflight(workflow, inflight, how, ended_at, now)

    def _end_inflight(self, workflow: str, inflight: dict[str, Any], how: str, ended_at: float,
                      now: float) -> None:
        inflight["controller_ended_at"] = ended_at
        entry = self.state["workflows"].setdefault(workflow, {})
        entry["inflight"] = None
        entry["last_inflight"] = dict(inflight, how=how)
        self.dirty = True
        deferred = sum(1 for tid, pane in self.state["panes"].items()
                       if pane.get("deferred") and self.workflow_key(tid) == workflow)
        log(f"controller ended workflow={workflow} event={inflight.get('event_id')} how={how} "
            f"ran={int(now - float(inflight.get('since') or now))}s deferred={deferred}")

    def alert_owner(self, key: str, text: str, now: float, min_interval: float) -> None:
        """Send one owner alert per key and interval (the times persist in the state file)."""
        sent = self.state.setdefault("alerts", {})
        if now - float(sent.get(key) or float("-inf")) < min_interval:
            return
        sent[key] = now
        if len(sent) > 200:  # keep the state file small
            for old in sorted(sent, key=sent.get)[:-200]:
                del sent[old]
        self.dirty = True
        log(f"alert key={key}")
        self._alert_sink(text)

    # -- watchdog ---------------------------------------------------------------------

    def _watchdog(self, now: float) -> None:
        if not self.cfg.watchdog:
            return
        if self.last_watchdog is not None and now - self.last_watchdog < self.cfg.watchdog_interval:
            return
        self.last_watchdog = now
        for task_id, task in self.registered.items():
            if task_id not in self.tasks or task_id.startswith("auto:"):
                continue
            if not task["policy"].get("allow_continue") or task["pane_id"] not in self.live:
                continue
            if self._workflow_busy(self.workflow_key(task_id)):
                continue
            pane = self.state["panes"].get(task_id)
            if not pane or not pane.get("last_seen") or pane.get("wake") or pane.get("deferred"):
                continue
            self._watch_task(task, pane, now)

    def _watch_task(self, task: dict[str, Any], pane: dict[str, Any], now: float) -> None:
        task_id = task["id"]
        status = pane["last_seen"]
        watch = pane.setdefault("watchdog", {})
        wait = self.wait_for(task_id)
        until = parse_time(wait["until"]) if wait["until"] else None
        if until is not None:
            if until > now:
                return
            if watch.get("wait_fired_until") != wait["until"] and status != "working":
                watch["wait_fired_until"] = wait["until"]
                self.dirty = True
                log(f"watchdog wait_elapsed task={task_id} until={wait['until']} wait_reason={wait['reason']}")
                self.request_wake(task, status, "wait_elapsed", now)
                return
        if wait["reason"] in WAIT_SUPPRESSES_STALL:
            return
        if status == "working":
            self._watch_working(task, pane, watch, now)
        elif status in IDLE_LIKE:
            self._watch_stall(task, pane, watch, now, until)

    def _watch_stall(self, task: dict[str, Any], pane: dict[str, Any], watch: dict[str, Any],
                     now: float, until: float | None) -> None:
        epoch = int(pane.get("real_work_epoch") or 0)
        if watch.get("stall_epoch") != epoch:
            watch["stall_epoch"] = epoch
            watch["stall_count"] = 0
            self.dirty = True
        count = int(watch.get("stall_count") or 0)
        start = float(pane.get("idle_since") or pane.get("status_since") or now)
        if until is not None and until <= now:
            start = max(start, until)
        idle_for = now - start
        if count >= 2:
            # Two controller wakes did not move it: tell the owner once instead of going silent.
            if idle_for >= self.cfg.stall_alert_seconds and watch.get("alerted_epoch") != epoch:
                watch["alerted_epoch"] = epoch
                self.dirty = True
                self.alert_owner(f"stall:{task['id']}:{epoch}", f"⚠️ {self.cfg.project}: task {task['id']} has been stuck for {int(idle_for // 3600)} h "
                f"{int(idle_for % 3600 // 60)} min; the controller could not move it twice. "
                f"Pane {task['pane_id']}: look at it or ask the bot for a status.", now, 0.0)
            return
        if idle_for < self.cfg.stall_seconds * (1 if count == 0 else 3):
            return
        watch["stall_count"] = count + 1
        self.dirty = True
        log(f"watchdog stall task={task['id']} status={pane['last_seen']} idle={int(idle_for)}s "
            f"n={count + 1}")
        self.request_wake(task, pane["last_seen"], "stall", now)

    def _watch_working(self, task: dict[str, Any], pane: dict[str, Any], watch: dict[str, Any],
                       now: float) -> None:
        epoch = int(pane.get("working_epoch") or 0)
        if watch.get("wno_epoch") == epoch:
            return
        if watch.get("rev_epoch") != epoch:
            watch.update(rev_epoch=epoch, revision=None, revision_polled_at=None,
                         revision_changed_at=float(pane.get("last_work_started") or now))
            self.dirty = True
        polled = watch.get("revision_polled_at")
        if polled is None or now - float(polled) >= self.cfg.revision_poll_seconds:
            try:
                revision = self._agent_get(task["pane_id"]).get("revision")
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                log(f"watchdog agent get failed task={task['id']} reason={type(exc).__name__}")
                return
            watch["revision_polled_at"] = now
            if revision != watch.get("revision"):
                watch["revision"] = revision
                watch["revision_changed_at"] = now
            self.dirty = True
        quiet = now - float(watch.get("revision_changed_at") or now)
        if quiet >= self.cfg.working_no_output_seconds:
            watch["wno_epoch"] = epoch
            self.dirty = True
            log(f"watchdog working_no_output task={task['id']} quiet={int(quiet)}s")
            self.request_wake(task, "working", "working_no_output", now)

    def idle(self, seconds: float) -> None:
        """Sleep while still running timers and deliveries (reconnect backoff)."""
        deadline = time.monotonic() + seconds
        while True:
            self.safe_tick(time.time())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(self.cfg.loop_interval, remaining))


# ---------------------------------------------------------------------------
# Socket loop
# ---------------------------------------------------------------------------


def connect_and_subscribe(socket_path: str, tasks: dict[str, dict[str, Any]],
                          timeout: float = 5.0) -> tuple[socket.socket, bytearray]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall((json.dumps(subscription_request(tasks)) + "\n").encode("utf-8"))
        buffer = bytearray()
        deadline = time.monotonic() + timeout
        while b"\n" not in buffer:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for subscription ack")
            chunk = client.recv(65536)
            if not chunk:
                raise ConnectionError("Herdr closed before subscription ack")
            buffer.extend(chunk)
        ack_bytes, _, remainder = buffer.partition(b"\n")
        ack = json.loads(ack_bytes.decode("utf-8", errors="replace"))
        if not isinstance(ack, dict) or ack.get("result", {}).get("type") != "subscription_started":
            raise ConnectionError(f"unexpected subscription ack: {_short(ack)}")
        client.setblocking(False)
        return client, bytearray(remainder)
    except BaseException:
        client.close()
        raise


def run(args: argparse.Namespace) -> int:
    cfg = BridgeConfig.from_args(args)
    bridge = Bridge(cfg, threaded=True)
    backoff = 1.0
    while True:
        if not bridge.prepare_subscription(time.time()):
            bridge.idle(min(backoff, 60.0))
            backoff = min(backoff * 2, 60.0)
            continue
        client: socket.socket | None = None
        try:
            client, buffer = connect_and_subscribe(cfg.socket, bridge.tasks)
            bridge.mark_subscribed(time.time())
            backoff = 1.0
            while not bridge.need_resubscribe:
                readable, _, _ = select.select([client], [], [], cfg.loop_interval)
                if readable:
                    try:
                        chunk = client.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        chunk = None
                    if chunk == b"":
                        raise ConnectionError("Herdr socket disconnected")
                    if chunk:
                        buffer.extend(chunk)
                        if len(buffer) > MAX_SOCKET_BUFFER:
                            raise ConnectionError("Herdr event line too long")
                # Also drains lines that arrived together with the subscription ack.
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    bridge.handle_line(bytes(line), time.time())
                bridge.safe_tick(time.time())
        except (OSError, ValueError, ConnectionError) as exc:
            log(f"bridge reconnect reason={type(exc).__name__}:{_short(exc)}")
            bridge.idle(min(backoff, 60.0))
            backoff = min(backoff * 2, 60.0)
        finally:
            if client is not None:
                client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--socket", default=default_socket())
    parser.add_argument("--config", default="",
                        help=f"per-profile {PIPELINE_CONFIG}: defaults for --project/--cwd-prefix/"
                             "--sibling-prefix/--webhook (explicit flags win)")
    parser.add_argument("--webhook", default=None)
    parser.add_argument("--hermes-bin", default=shutil.which("hermes") or "hermes")
    parser.add_argument("--herdr-bin", default=shutil.which("herdr") or "herdr")
    parser.add_argument("--session", default="default")
    parser.add_argument("--project", default=None)
    parser.add_argument("--profile", default=None, help="Hermes profile the route is bound to (default: from --config)")
    parser.add_argument("--alert-chat", default=None, help="Telegram chat for owner alerts (default: chat_id from --config)")
    parser.add_argument("--alert-env", default=None, help=".env holding TELEGRAM_BOT_TOKEN (default: next to --config)")
    parser.add_argument("--cwd-prefix", action="append", default=None)
    parser.add_argument("--sibling-prefix", action="store_true", default=None)
    parser.add_argument("--registry-poll", type=float, default=1.0,
                        help="main loop / registry mtime poll interval (capped at 1s)")
    parser.add_argument("--webhook-timeout", type=int, default=MAX_WEBHOOK_TIMEOUT,
                        help=f"per-attempt delivery timeout (capped at {MAX_WEBHOOK_TIMEOUT}s)")
    parser.add_argument("--state-db", default="",
                        help="gateway state.db (default: $HERMES_HOME/state.db, else <registry>/../../state.db)")
    parser.add_argument("--debounce-seconds", type=float, default=20.0)
    parser.add_argument("--blocked-debounce-seconds", type=float, default=3.0)
    parser.add_argument("--unknown-grace-seconds", type=float, default=45.0)
    parser.add_argument("--hold-seconds", type=float, default=900.0)
    parser.add_argument("--stall-seconds", type=float, default=900.0)
    parser.add_argument("--working-no-output-seconds", type=float, default=1200.0)
    parser.add_argument("--watchdog-interval", type=float, default=60.0)
    parser.add_argument("--no-watchdog", action="store_true", help="disable stall/wait watchdog wakes")
    return apply_pipeline_config(parser.parse_args(argv))


def apply_pipeline_config(args: argparse.Namespace) -> argparse.Namespace:
    """Fill route options not given on the command line from --config."""
    cfg: dict[str, Any] = {}
    if args.config:
        loaded = load_pipeline_config(args.config)
        if loaded is None:
            raise SystemExit(f"invalid or missing pipeline config: {args.config}")
        cfg = loaded
    if args.project is None:
        args.project = cfg.get("project", "")
    if getattr(args, "profile", None) is None:
        args.profile = cfg.get("profile", "")
    if getattr(args, "alert_chat", None) is None:
        args.alert_chat = cfg.get("chat_id", "")
    if getattr(args, "alert_env", None) is None:
        args.alert_env = str(Path(args.config).parent / ".env") if args.config else ""
    if args.webhook is None:
        args.webhook = cfg.get("route", "herdr-agent-events")
    if args.cwd_prefix is None:
        args.cwd_prefix = list(cfg.get("cwd_prefixes", []))
    if args.sibling_prefix is None:
        args.sibling_prefix = bool(cfg.get("sibling_prefix", False))
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
