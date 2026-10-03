"""Profile-local Telegram/Hermes bridge for Herdr.

The bridge never relies on Herdr UI focus or --current. Every operation targets
an explicit named session and opaque object/agent identifier. Subprocesses use
argv arrays (no shell) and bounded output/timeouts.

Prompts/text sent to agent panes pass a busy/duplicate guard backed by a small
locked send journal (state/herdr_sends.json). The task registry is only edited
through scripts/herdr_registry.py (tool ``herdr_task``).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

HERDR = shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")
MAX_OUTPUT = 50_000
MAX_TEXT = 20_000
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:.@/-]{0,127}$")
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AGENT_KINDS = {
    "pi", "claude", "codex", "gemini", "cursor", "devin", "agy", "cline",
    "omp", "mastracode", "opencode", "copilot", "kimi", "kiro", "droid",
    "amp", "grok", "hermes", "kilo", "qodercli", "maki",
}
STATES = {"idle", "working", "blocked", "done", "unknown"}
SOURCES = {"visible", "recent", "recent-unwrapped", "detection"}
DIRECTIONS = {"right", "down"}

# Canonical key names accepted by `herdr ... send-keys` (herdr rejects
# home/end with invalid_key; those get a hint instead).
_CTRL_LETTERS = "abcdefghklnprstuwxyz"
ALLOWED_KEYS: list[str] = [
    "enter", "esc", "tab", "backtab", "up", "down", "left", "right",
    "pageup", "pagedown", "backspace", "delete", "space",
    *[f"ctrl+{c}" for c in _CTRL_LETTERS],
    *list("0123456789"), "y", "n",
]
SAFE_KEYS = frozenset(ALLOWED_KEYS)
_KEY_ALIASES = {
    "return": "enter", "ret": "enter", "cr": "enter", "newline": "enter",
    "escape": "esc", "bs": "backspace", "del": "delete", "spacebar": "space",
    "shifttab": "backtab", "stab": "backtab", "pgup": "pageup", "pgdn": "pagedown",
    "pgdown": "pagedown",
}
_KEY_HINTS = {"home": "ctrl+a", "end": "ctrl+e"}
_KEYS_HELP = "enter, esc, tab, backtab, up, down, left, right, pageup, pagedown, backspace, delete, space, ctrl+<a-h,k,l,n,p,r-u,w-z>, 0-9, y, n"

# Code sits next to this plugin (herdr-core when symlinked into a profile);
# state belongs to the profile whose gateway imports it.
CODE_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = CODE_ROOT / "scripts"
PROFILE_HOME = Path(get_hermes_home())
REGISTRY_PATH = PROFILE_HOME / "state" / "herdr_tasks.json"
SENDS_PATH = PROFILE_HOME / "state" / "herdr_sends.json"
MAX_JOURNAL = 200
DUPLICATE_WINDOW_S = 600.0
RECENT_SEND_S = 5.0
DUP_MIN_TEXT_CHARS = 16  # shorter pane_send_text is keystroke-like (menu digits, y/n)
STATUS_TIMEOUT_MS = 5_000
FRESH_TIMEOUT_S = 20.0
FRESH_POLL_S = 1.0


def _available() -> bool:
    return Path(HERDR).is_file() and os.access(HERDR, os.X_OK)


def _clean_session(value: Any) -> str:
    session = str(value or "default").strip()
    if not SESSION_RE.fullmatch(session):
        raise ValueError("invalid session name")
    return session


def _clean_target(value: Any, label: str = "target") -> str:
    target = str(value or "").strip()
    if not TARGET_RE.fullmatch(target):
        raise ValueError(f"invalid or missing {label}")
    return target


def _text(value: Any, label: str, *, required: bool = True) -> str:
    text = str(value or "")
    if required and not text.strip():
        raise ValueError(f"missing {label}")
    if len(text) > MAX_TEXT or "\x00" in text:
        raise ValueError(f"{label} is too long or contains NUL")
    return text


def _bounded_int(value: Any, default: int, low: int, high: int, label: str) -> int:
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc
    if not low <= number <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return number


def _cwd(value: Any) -> str:
    path = Path(_text(value, "cwd")).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    return str(path.resolve())


def _normalize_key(raw: Any) -> str:
    """Map common spellings (CTRL_U, C-u, ArrowUp, RETURN...) to herdr names."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError(f"key {raw!r} must be a string")
    text = str(raw)
    if text == " ":
        return "space"
    key = text.strip().lower()
    if key in SAFE_KEYS:
        return key
    joined = re.sub(r"[\s_\-+]+", "+", key).strip("+")
    bare = joined.replace("+", "")
    match = (re.fullmatch(r"(?:ctrl|control|ctl|c)\+([a-z])", joined)
             or re.fullmatch(r"\^([a-z])", joined)
             or re.fullmatch(r"(?:ctrl|control)([a-z])", bare))
    if match:
        candidate = f"ctrl+{match.group(1)}"
    elif re.fullmatch(r"(?:arrow)?(up|down|left|right)(?:arrow)?", bare):
        candidate = re.sub(r"arrow", "", bare)
    elif re.fullmatch(r"page(up|down)", bare):
        candidate = bare
    else:
        candidate = _KEY_ALIASES.get(bare, bare)
    if candidate in SAFE_KEYS:
        return candidate
    if candidate in _KEY_HINTS:
        raise ValueError(f"key {text!r} is not supported by herdr; use {_KEY_HINTS[candidate]}")
    raise ValueError(f"key {text!r} is not allowed; allowed: {_KEYS_HELP}")


def _argv(session: str, *parts: str) -> list[str]:
    # --session is explicit by design: the bridge never follows another UI's focus.
    return [HERDR, "--session", session, *parts]


def _safe_command(argv: list[str]) -> list[str]:
    safe = [Path(argv[0]).name, *argv[1:]]
    # Do not echo prompts, terminal commands, or literal text into gateway logs.
    # All bridge session commands have a fixed prefix:
    # herdr --session NAME GROUP VERB TARGET [TEXT]. Do not search with
    # list.index(): a valid session may itself be named "agent" or "pane".
    if len(safe) >= 7 and safe[1] == "--session":
        group, verb = safe[3], safe[4]
        if (group, verb) in {("agent", "prompt"), ("pane", "run"), ("pane", "send-text")}:
            safe[6] = f"<redacted:{len(safe[6])} chars>"
        elif (group, verb) == ("pane", "wait-output") and len(safe) >= 8 and safe[6] == "--match":
            safe[7] = f"<redacted:{len(safe[7])} chars>"
    return safe


def _partial_text(value: Any) -> str:
    """Normalize TimeoutExpired partial output (bytes even with text=True)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run(argv: list[str], timeout_ms: int = 30_000) -> dict[str, Any]:
    timeout_s = max(1.0, min(timeout_ms / 1000.0 + 2.0, 305.0))
    safe_command = _safe_command(argv)
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "error": "bridge_timeout",
            "command": safe_command,
            "timeout_ms": timeout_ms,
            "output": _partial_text(exc.stdout)[-MAX_OUTPUT:],
        }
    stdout = (proc.stdout or "")[-MAX_OUTPUT:]
    stderr = (proc.stderr or "")[-MAX_OUTPUT:]
    data: Any = stdout.strip()
    if data:
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass
    result: dict[str, Any] = {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "command": safe_command,
        "data": data,
    }
    if stderr.strip():
        try:
            result["error"] = json.loads(stderr)
        except json.JSONDecodeError:
            result["error"] = stderr.strip()
    return result


def _response(fn) -> str:
    try:
        return json.dumps(fn(), ensure_ascii=False)
    except ValueError as exc:
        return json.dumps({"success": False, "error": "validation_error", "message": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # fail closed without leaking traceback to chat
        return json.dumps({"success": False, "error": "bridge_error", "message": str(exc)}, ensure_ascii=False)


def _herdr_error_code(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        inner = error.get("error")
        if isinstance(inner, dict):
            return str(inner.get("code") or "")
        return str(error.get("code") or "")
    return str(error or "")


# --------------------------------------------------------------------------
# live agent status + send journal (busy / duplicate guard)
# --------------------------------------------------------------------------

def _agent_info(session: str, target: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return (agent dict, raw result) from `herdr agent get`; agent is None on failure."""
    try:
        result = _run(_argv(session, "agent", "get", target), STATUS_TIMEOUT_MS)
    except OSError as exc:
        return None, {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    data = result.get("data")
    agent = data.get("result", {}).get("agent") if result.get("success") and isinstance(data, dict) else None
    return (agent if isinstance(agent, dict) else None), result


def _agent_state(agent: dict[str, Any] | None) -> dict[str, Any]:
    agent = agent or {}
    session_info = agent.get("agent_session")
    return {
        "pane_id": agent.get("pane_id"),
        "agent_status": agent.get("agent_status"),
        "agent_session": session_info.get("value") if isinstance(session_info, dict) else session_info,
        "revision": agent.get("revision"),
        "state_change_seq": agent.get("state_change_seq"),
    }


@contextlib.contextmanager
def _journal(timeout_s: float = 5.0):
    """Exclusive, bounded send journal. Yields the mutable state; saved on exit."""
    SENDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(SENDS_PATH.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                    raise RuntimeError("send journal is busy; retry") from exc
                time.sleep(0.05)
        try:
            state = json.loads(SENDS_PATH.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("entries"), list):
                raise ValueError
        except (OSError, ValueError):
            state = {"version": 1, "entries": []}
        yield state
        state["entries"] = [e for e in state["entries"] if isinstance(e, dict)][-MAX_JOURNAL:]
        temp = SENDS_PATH.with_name(f".{SENDS_PATH.name}.tmp-{os.getpid()}")
        with open(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, SENDS_PATH)
    finally:
        os.close(fd)


def _send_outcome(result: dict[str, Any]) -> str:
    if result.get("success"):
        return "sent"
    if result.get("error") == "bridge_timeout" or _herdr_error_code(result) in {"timeout", "agent_prompt_stalled"}:
        return "uncertain"  # submitted, only the settle-wait failed
    return "failed"


def _guard_refusal(entries: list[dict[str, Any]], *, session: str, pane: str, agent_session: str,
                   status: str | None, digest: str, action: str, check_recent: bool,
                   check_duplicate: bool) -> dict[str, Any] | None:
    now = time.time()
    if status == "working":
        return {"error": "target_busy", "reason": "agent_status=working",
                "message": "Target is working; do not interrupt. Wait for idle/done, or force=true after inspecting."}
    if status == "blocked" and action in {"agent_prompt", "fresh_session"}:
        return {"error": "target_blocked", "reason": "agent_status=blocked",
                "message": "Target shows a prompt/menu; inspect it (herdr_inspect agent_read) and answer deliberately."}
    same_target = [e for e in entries if e.get("session") == session and e.get("pane_id") == pane]
    if check_recent:
        for entry in same_target:
            if entry.get("status") == "reserved" and now < float(entry.get("hold_until") or 0):
                return {"error": "target_busy", "reason": "send_in_flight",
                        "message": "Another controller is sending to this target right now."}
            # Keystroke-like texts and fresh_session (verified idle) do not arm the window.
            if (entry.get("status") in {"sent", "uncertain"} and entry.get("substantive", True)
                    and entry.get("action") != "fresh_session"
                    and now - float(entry.get("ts") or 0) < RECENT_SEND_S):
                return {"error": "target_busy", "reason": "recent_send",
                        "retry_after_s": round(RECENT_SEND_S - (now - float(entry.get("ts") or 0)), 1),
                        "message": "A prompt was just delivered; re-check status before sending more."}
    if check_duplicate:
        for entry in same_target:
            age = now - float(entry.get("ts") or 0)
            if (entry.get("sha256") == digest and entry.get("agent_session", "") == agent_session
                    and entry.get("status") in {"reserved", "sent", "uncertain"} and age < DUPLICATE_WINDOW_S):
                return {"error": "duplicate_send", "age_s": round(age, 1), "previous_action": entry.get("action"),
                        "message": "Identical text was already sent to this agent session within 10 minutes."}
    return None


def _guarded_send(action: str, session: str, target: str, text: str, cmd: list[str],
                  timeout_ms: int, *, force: bool, hold_s: float) -> dict[str, Any]:
    """Busy/duplicate-guarded send to an agent pane (plain panes pass through)."""
    agent, probe = _agent_info(session, target)
    if agent is None:
        code = _herdr_error_code(probe)
        if action == "pane_send_text" and code == "agent_not_found":
            return _run(cmd, timeout_ms)  # plain shell pane: no agent guard
        if not force:
            return {"success": False, "error": "target_not_agent" if code == "agent_not_found" else "status_check_failed",
                    "target": target, "detail": probe.get("error")}
    before = _agent_state(agent)
    pane = str(before["pane_id"] or target)
    agent_session = str(before["agent_session"] or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    substantive = action != "pane_send_text" or len(text.strip()) >= DUP_MIN_TEXT_CHARS
    entry_id = uuid.uuid4().hex[:12]
    with _journal() as journal:
        if not force:
            refusal = _guard_refusal(
                journal["entries"], session=session, pane=pane, agent_session=agent_session,
                status=before["agent_status"], digest=digest, action=action,
                check_recent=substantive, check_duplicate=substantive and action != "fresh_session")
            if refusal:
                return {"success": False, **refusal, "target": target, "sent": False, "status_before": before}
        now = time.time()
        journal["entries"].append({
            "id": entry_id, "ts": now, "hold_until": now + hold_s, "action": action,
            "session": session, "pane_id": pane, "agent_session": agent_session,
            "sha256": digest, "chars": len(text), "status": "reserved", "forced": bool(force),
            "substantive": substantive,
        })
    result = _run(cmd, timeout_ms)
    outcome = _send_outcome(result)
    with _journal() as journal:
        for entry in journal["entries"]:
            if entry.get("id") == entry_id:
                entry["status"] = outcome
                entry["ts"] = time.time()
    result["status_before"] = before
    if outcome != "failed" and agent is not None:
        after, _ = _agent_info(session, target)
        result["post_send"] = _agent_state(after) if after else {"agent_status": "unavailable"}
    return result


def _fresh_session(session: str, target: str, *, force: bool, task_id: Any) -> dict[str, Any]:
    agent, probe = _agent_info(session, target)
    if agent is None:
        code = _herdr_error_code(probe)
        return {"success": False, "error": "target_not_agent" if code == "agent_not_found" else "status_check_failed",
                "target": target, "detail": probe.get("error")}
    old = _agent_state(agent)
    sent = _guarded_send("fresh_session", session, target, "/clear",
                         _argv(session, "agent", "prompt", target, "/clear"), 10_000,
                         force=force, hold_s=FRESH_TIMEOUT_S + 10)
    if not sent.get("success"):
        return {**sent, "old_session": old["agent_session"]}
    deadline = time.monotonic() + FRESH_TIMEOUT_S
    latest = old
    while True:
        time.sleep(FRESH_POLL_S)
        current, _ = _agent_info(session, target)
        if current is not None:
            latest = _agent_state(current)
            if (latest["agent_session"] and latest["agent_session"] != old["agent_session"]
                    and latest["agent_status"] in {"idle", "done"}):
                break
        if time.monotonic() >= deadline:
            break
    changed = bool(latest["agent_session"]) and latest["agent_session"] != old["agent_session"]
    result: dict[str, Any] = {
        "success": changed, "changed": changed, "old_session": old["agent_session"],
        "new_session": latest["agent_session"], "pane_id": latest["pane_id"] or old["pane_id"],
        "agent_status": latest["agent_status"], "revision": latest["revision"],
    }
    if not changed:
        result.update(error="session_not_reset",
                      message=f"agent_session unchanged after {int(FRESH_TIMEOUT_S)}s; inspect the pane")
    elif task_id:
        try:
            result["registry"] = _registry().update(
                str(task_id), {"agent_session": latest["agent_session"],
                               "previous_agent_session": old["agent_session"] or ""},
                "herdr_control.fresh_session", registry=REGISTRY_PATH)
        except Exception as exc:  # the session reset itself succeeded
            result["registry"] = _registry_error(exc)
    return result


# --------------------------------------------------------------------------
# task registry (scripts/herdr_registry.py)
# --------------------------------------------------------------------------

_REGISTRY_CACHE: tuple[int, Any] | None = None


def _registry():
    """Load scripts/herdr_registry.py by path (reloaded when the file changes)."""
    global _REGISTRY_CACHE
    path = SCRIPTS_DIR / "herdr_registry.py"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise RuntimeError(f"registry library missing: {path}") from exc
    if _REGISTRY_CACHE is None or _REGISTRY_CACHE[0] != mtime:
        spec = importlib.util.spec_from_file_location("herdr_core_registry", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _REGISTRY_CACHE = (mtime, module)
    return _REGISTRY_CACHE[1]


def _registry_error(exc: Exception) -> dict[str, Any]:
    as_dict = getattr(exc, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    code = "validation_error" if isinstance(exc, ValueError) else "registry_error"
    return {"success": False, "error": code, "message": str(exc)[:500]}


def _live_panes(session: str) -> set[str] | None:
    try:
        result = _run(_argv(session, "pane", "list"), STATUS_TIMEOUT_MS)
    except OSError:
        return None
    data = result.get("data")
    panes = data.get("result", {}).get("panes") if result.get("success") and isinstance(data, dict) else None
    if not isinstance(panes, list):
        return None
    return {str(p.get("pane_id")) for p in panes if isinstance(p, dict) and p.get("pane_id")}


def task(args: dict[str, Any], **_: Any) -> str:
    """herdr_task: locked registry reads/writes. Never raises."""
    try:
        reg = _registry()
        action = str(args.get("action") or "")
        task_id = args.get("task_id")
        by = str(args.get("by") or "controller")
        if action == "list":
            result = reg.list_tasks(not args.get("all"), REGISTRY_PATH)
        elif not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id is required")
        elif action == "get":
            result = reg.get(task_id.strip(), REGISTRY_PATH)
        elif action == "update":
            expect = args.get("expect_generation")
            result = reg.update(task_id.strip(), args.get("fields"), by,
                                expect_generation=expect if isinstance(expect, int) and not isinstance(expect, bool) else None,
                                registry=REGISTRY_PATH)
        elif action == "note":
            result = reg.add_note(task_id.strip(), args.get("text"), by, REGISTRY_PATH)
        elif action == "decision":
            result = reg.add_decision(task_id.strip(), args.get("text"), by, REGISTRY_PATH)
        elif action == "create":
            fields = args.get("fields") or {}
            if not isinstance(fields, dict):
                raise ValueError("fields must be an object")
            result = reg.create({**fields, "id": task_id.strip()}, by, REGISTRY_PATH)
        elif action == "archive":
            try:
                current = reg.load(REGISTRY_PATH)
                session = next((str(t.get("session") or "default") for t in current["tasks"]
                                if isinstance(t, dict) and t.get("id") == task_id.strip()), "default")
                live = _live_panes(_clean_session(session)) if _available() else None
            except Exception:
                live = None  # without a live pane list, tombstone stubs are kept
            result = reg.archive(task_id.strip(), live, reason=str(args.get("text") or "archived")[:200],
                                 registry=REGISTRY_PATH)
        else:
            raise ValueError("unsupported herdr_task action")
    except Exception as exc:
        result = _registry_error(exc)
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return json.dumps({"success": False, "error": "registry_error", "message": str(exc)})


def _registry_available() -> bool:
    return (SCRIPTS_DIR / "herdr_registry.py").is_file()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def inspect(args: dict[str, Any], **_: Any) -> str:
    """Read-only Herdr inspection and bounded waits."""
    def go() -> dict[str, Any]:
        action = str(args.get("action") or "status")
        session = _clean_session(args.get("session"))
        target = args.get("target")
        workspace = args.get("workspace")
        lines = _bounded_int(args.get("lines"), 120, 1, 1000, "lines")
        timeout_ms = _bounded_int(args.get("timeout_ms"), 30_000, 100, 300_000, "timeout_ms")

        if action == "status":
            return _run(_argv(session, "status"), timeout_ms)
        if action == "sessions":
            return _run([HERDR, "session", "list"], timeout_ms)
        if action == "snapshot":
            return _run(_argv(session, "api", "snapshot"), timeout_ms)
        if action == "workspaces":
            return _run(_argv(session, "workspace", "list"), timeout_ms)
        if action == "workspace_get":
            return _run(_argv(session, "workspace", "get", _clean_target(target)), timeout_ms)
        if action == "tabs":
            cmd = _argv(session, "tab", "list")
            if workspace:
                cmd += ["--workspace", _clean_target(workspace, "workspace")]
            return _run(cmd, timeout_ms)
        if action == "tab_get":
            return _run(_argv(session, "tab", "get", _clean_target(target)), timeout_ms)
        if action == "panes":
            cmd = _argv(session, "pane", "list")
            if workspace:
                cmd += ["--workspace", _clean_target(workspace, "workspace")]
            return _run(cmd, timeout_ms)
        if action == "pane_get":
            return _run(_argv(session, "pane", "get", _clean_target(target)), timeout_ms)
        if action == "pane_layout":
            return _run(_argv(session, "pane", "layout", "--pane", _clean_target(target)), timeout_ms)
        if action == "pane_process":
            return _run(_argv(session, "pane", "process-info", "--pane", _clean_target(target)), timeout_ms)
        if action == "pane_read":
            source = str(args.get("source") or "recent-unwrapped")
            if source not in SOURCES:
                raise ValueError("invalid source")
            return _run(_argv(session, "pane", "read", _clean_target(target), "--source", source, "--lines", str(lines)), timeout_ms)
        if action == "agents":
            return _run(_argv(session, "agent", "list"), timeout_ms)
        if action == "agent_get":
            return _run(_argv(session, "agent", "get", _clean_target(target)), timeout_ms)
        if action == "agent_read":
            source = str(args.get("source") or "recent-unwrapped")
            if source not in SOURCES:
                raise ValueError("invalid source")
            return _run(_argv(session, "agent", "read", _clean_target(target), "--source", source, "--lines", str(lines)), timeout_ms)
        if action == "agent_explain":
            return _run(_argv(session, "agent", "explain", _clean_target(target), "--format", "json"), timeout_ms)
        if action == "agent_wait":
            cmd = _argv(session, "agent", "wait", _clean_target(target))
            states = args.get("states") or []
            if isinstance(states, str):
                states = [states]
            for state in states:
                if state not in STATES:
                    raise ValueError("invalid agent state")
                cmd += ["--until", state]
            cmd += ["--timeout", str(timeout_ms)]
            return _run(cmd, timeout_ms)
        if action == "pane_wait_output":
            literal = _text(args.get("match"), "match")
            return _run(_argv(session, "pane", "wait-output", _clean_target(target), "--match", literal, "--source", "recent-unwrapped", "--lines", str(lines), "--timeout", str(timeout_ms)), timeout_ms)
        raise ValueError("unsupported inspect action")
    return _response(go)


def control(args: dict[str, Any], **_: Any) -> str:
    """Explicitly requested, non-destructive Herdr mutations."""
    def go() -> dict[str, Any]:
        action = str(args.get("action") or "")
        session = _clean_session(args.get("session"))
        target = args.get("target")
        timeout_ms = _bounded_int(args.get("timeout_ms"), 30_000, 100, 300_000, "timeout_ms")
        force = args.get("force") is True

        if action == "workspace_create":
            cmd = _argv(session, "workspace", "create", "--cwd", _cwd(args.get("cwd")), "--no-focus")
            if args.get("label"):
                cmd += ["--label", _text(args.get("label"), "label")]
            return _run(cmd, timeout_ms)
        if action == "tab_create":
            cmd = _argv(session, "tab", "create", "--workspace", _clean_target(args.get("workspace"), "workspace"), "--cwd", _cwd(args.get("cwd")), "--no-focus")
            if args.get("label"):
                cmd += ["--label", _text(args.get("label"), "label")]
            return _run(cmd, timeout_ms)
        if action == "pane_split":
            direction = str(args.get("direction") or "right")
            if direction not in DIRECTIONS:
                raise ValueError("direction must be right or down")
            cmd = _argv(session, "pane", "split", _clean_target(target), "--direction", direction, "--no-focus")
            if args.get("cwd"):
                cmd += ["--cwd", _cwd(args.get("cwd"))]
            return _run(cmd, timeout_ms)
        if action == "pane_run":
            return _run(_argv(session, "pane", "run", _clean_target(target), _text(args.get("text"), "command")), timeout_ms)
        if action == "pane_send_text":
            clean = _clean_target(target)
            text = _text(args.get("text"), "text")
            return _guarded_send(action, session, clean, text, _argv(session, "pane", "send-text", clean, text),
                                 timeout_ms, force=force, hold_s=30.0)
        if action in {"pane_send_keys", "agent_send_keys"}:
            keys = args.get("keys") or []
            if isinstance(keys, (str, int)):
                keys = [keys]
            if not isinstance(keys, list) or not keys or len(keys) > 200:
                raise ValueError("keys must be a list of 1-200 key names")
            normalized = [_normalize_key(k) for k in keys]
            group = "pane" if action.startswith("pane_") else "agent"
            return _run(_argv(session, group, "send-keys", _clean_target(target), *normalized), timeout_ms)
        if action == "agent_start":
            name = str(args.get("name") or "")
            kind = str(args.get("kind") or "")
            if not AGENT_RE.fullmatch(name):
                raise ValueError("invalid agent name")
            if kind not in AGENT_KINDS:
                raise ValueError("unsupported agent kind")
            return _run(_argv(session, "agent", "start", name, "--kind", kind, "--pane", _clean_target(target), "--timeout", str(timeout_ms)), timeout_ms)
        if action == "agent_prompt":
            clean = _clean_target(target)
            text = _text(args.get("text"), "prompt")
            cmd = _argv(session, "agent", "prompt", clean, text)
            wait = args.get("wait") is True
            if wait:
                cmd += ["--wait", "--timeout", str(timeout_ms)]
                states = args.get("states") or []
                if isinstance(states, str):
                    states = [states]
                for state in states:
                    if state not in STATES:
                        raise ValueError("invalid agent state")
                    cmd += ["--until", state]
            return _guarded_send(action, session, clean, text, cmd, timeout_ms, force=force,
                                 hold_s=(timeout_ms / 1000.0 + 15.0) if wait else 30.0)
        if action == "fresh_session":
            return _fresh_session(session, _clean_target(target), force=force, task_id=args.get("task_id"))
        raise ValueError("unsupported control action")
    return _response(go)


def destructive(args: dict[str, Any], **_: Any) -> str:
    """Confirmed close/stop/delete operations only."""
    def go() -> dict[str, Any]:
        if args.get("confirm") is not True:
            raise ValueError("confirm=true is required after exact-scope user confirmation")
        action = str(args.get("action") or "")
        session = _clean_session(args.get("session"))
        target = args.get("target")
        if action == "pane_close":
            return _run(_argv(session, "pane", "close", _clean_target(target)))
        if action == "tab_close":
            return _run(_argv(session, "tab", "close", _clean_target(target)))
        if action == "workspace_close":
            return _run(_argv(session, "workspace", "close", _clean_target(target)))
        if action == "session_stop":
            name = _clean_session(target or session)
            return _run([HERDR, "session", "stop", name])
        if action == "session_delete":
            name = _clean_session(target or session)
            return _run([HERDR, "session", "delete", name])
        raise ValueError("unsupported destructive action")
    return _response(go)


INSPECT_SCHEMA = {
    "name": "herdr_inspect",
    "description": "Inspect an explicit Herdr session without relying on UI focus. Use for status, session/workspace/tab/pane/agent lists, bounded terminal reads, process info, detection explanation, and waits. Read-only and safe by default.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "sessions", "snapshot", "workspaces", "workspace_get", "tabs", "tab_get", "panes", "pane_get", "pane_layout", "pane_process", "pane_read", "agents", "agent_get", "agent_read", "agent_explain", "agent_wait", "pane_wait_output"]},
            "session": {"type": "string", "default": "default"},
            "target": {"type": "string", "description": "Opaque pane/tab/workspace ID or unique live agent name."},
            "workspace": {"type": "string"},
            "source": {"type": "string", "enum": ["visible", "recent", "recent-unwrapped", "detection"]},
            "lines": {"type": "integer", "minimum": 1, "maximum": 1000},
            "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 300000},
            "states": {"type": "array", "items": {"type": "string", "enum": ["idle", "working", "blocked", "done", "unknown"]}},
            "match": {"type": "string"}
        },
        "required": ["action"]
    }
}

CONTROL_SCHEMA = {
    "name": "herdr_control",
    "description": (
        "Perform a non-destructive Herdr mutation for a registered task (owner request or controller event). "
        "Uses an explicit session and target; never follows UI focus. Can create layout, run a command, start an agent, prompt it, "
        "send deliberate text/keys, or reset a Claude Code pane (fresh_session: /clear + verified new agent_session). "
        "agent_prompt/pane_send_text to an agent refuse target_busy (working/in-flight), target_blocked, or "
        "duplicate_send (same text, same agent session, <10 min) unless force=true; success returns post_send "
        "status/revision. Inspect blocked agents before input and never auto-approve permissions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["workspace_create", "tab_create", "pane_split", "pane_run", "pane_send_text", "pane_send_keys", "agent_start", "agent_prompt", "agent_send_keys", "fresh_session"]},
            "session": {"type": "string", "default": "default"},
            "target": {"type": "string"},
            "workspace": {"type": "string"},
            "cwd": {"type": "string", "description": "Existing absolute directory."},
            "label": {"type": "string"},
            "direction": {"type": "string", "enum": ["right", "down"]},
            "text": {"type": "string"},
            "keys": {"type": "array", "items": {"type": "string", "enum": ALLOWED_KEYS},
                     "description": "Canonical names (aliases like CTRL_U/ArrowUp/Return are normalized). No home/end: use ctrl+a/ctrl+e."},
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": sorted(AGENT_KINDS)},
            "wait": {"type": "boolean", "default": False,
                     "description": "agent_prompt: block until the agent settles. Default false; prefer herdr_inspect agent_wait."},
            "states": {"type": "array", "items": {"type": "string", "enum": sorted(STATES)}},
            "force": {"type": "boolean", "default": False, "description": "Bypass busy/duplicate guards after inspecting the target."},
            "task_id": {"type": "string", "description": "fresh_session: registry task whose agent_session to update."},
            "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 300000}
        },
        "required": ["action"]
    }
}

DESTRUCTIVE_SCHEMA = {
    "name": "herdr_destructive",
    "description": "Close or delete Herdr objects. Requires confirm=true. Allowed: (a) after the owner confirms the exact IDs, or (b) pane_close of the pane of a task already marked completed in the registry, after its agent exited (standing owner permission). Never for other panes or convenience.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["pane_close", "tab_close", "workspace_close", "session_stop", "session_delete"]},
            "session": {"type": "string", "default": "default"},
            "target": {"type": "string"},
            "confirm": {"type": "boolean"}
        },
        "required": ["action", "confirm"]
    }
}

_STR = {"type": "string"}
TASK_SCHEMA = {
    "name": "herdr_task",
    "description": (
        "Read/update the Herdr task registry (state/herdr_tasks.json) under a lock with validation. Use this "
        "instead of editing the JSON with patch/write_file/terminal. get=compact task view; list=active tasks; "
        "update=set whitelisted fields; note/decision=append short entries (decision task_id '*' = global); "
        "create; archive=move to archive jsonl (keeps a tombstone)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "list", "update", "note", "decision", "create", "archive"]},
            "task_id": {"type": "string"},
            "fields": {
                "type": "object",
                "description": (
                    "update/create. Unknown keys are rejected. policy merges partially; null removes an optional "
                    "key; phase_generation accepts \"+1\". Also accepted (legacy workflow keys, <=2000 chars JSON): "
                    "handoff, current_review_handoff, controller_disposition, review_gate, review_verdict, "
                    "final_artifact, expected_review_artifact, previous_review_artifact, last_controller_event, "
                    "last_controller_checkpoint, flow_checkpoint, controller_gate, controller_findings_current, "
                    "fresh_session_verified, latest_user_decision, decision_ledger, deployment_scope, "
                    "budget_gate_reconciliation."
                ),
                "properties": {
                    "enabled": {"type": "boolean"}, "completed": {"type": "boolean"},
                    "session": _STR, "workspace_id": _STR, "tab_id": _STR, "pane_id": _STR, "agent": _STR,
                    "agent_session": _STR, "previous_agent_session": _STR, "project": _STR, "cwd": _STR,
                    "change": _STR, "phase": {"type": "string", "description": "<=300 chars"},
                    "phase_generation": {"type": ["integer", "string"]}, "branch": _STR,
                    "execution_profile": {"type": "string", "enum": ["micro", "standard", "high-risk", "research"]},
                    "changed_files": {"type": "array", "items": _STR},
                    "risk_flags": {"type": "object"},
                    "policy": {"type": "object", "properties": {k: {"type": "boolean"} for k in (
                        "allow_continue", "allow_tests", "allow_deploy", "allow_commit", "allow_push",
                        "allow_sync_archive")}},
                    "candidate_sha": _STR, "deploy_revision": _STR, "evidence_manifest": _STR,
                    "parent_task_id": _STR, "parent_pane_id": _STR, "reviewer_pane_id": _STR, "reviewer_task_id": _STR,
                    "brief": {"type": "string", "description": "<=2000 chars"}, "brief_path": _STR,
                    "kanban_task_id": _STR,
                    "wait": {"type": "object", "properties": {
                        "reason": {"type": "string", "enum": ["none", "user_decision", "external", "scheduled", "complete"]},
                        "until": {"type": "string", "description": "ISO-8601 UTC or empty"},
                        "note": {"type": "string", "description": "<=300 chars"},
                        "quiet": {"type": "boolean", "description": "Long automated run only: hold the agent's short "
                                  "progress turns until `until`. Omit when the agent's next turn is a result."}}},
                },
            },
            "text": {"type": "string", "description": "note/decision text (<=600 chars); archive reason."},
            "by": {"type": "string", "description": "Author/source label, e.g. controller or user."},
            "expect_generation": {"type": "integer", "description": "update: fail unless phase_generation equals this."},
            "all": {"type": "boolean", "description": "list: include disabled/archived."}
        },
        "required": ["action"]
    }
}


def _slash(raw_args: str) -> str:
    argv = (raw_args or "").strip().split()
    sub = argv[0].lower() if argv else "status"
    session = argv[1] if len(argv) > 1 else "default"
    aliases = {
        "status": "status", "sessions": "sessions", "snapshot": "snapshot",
        "workspaces": "workspaces", "tabs": "tabs", "panes": "panes", "agents": "agents",
    }
    if sub not in aliases:
        return "Usage: /herdr [status|sessions|snapshot|workspaces|tabs|panes|agents] [session]"
    result = json.loads(inspect({"action": aliases[sub], "session": session}))
    return json.dumps(result, ensure_ascii=False, indent=2)


OWNER_QUESTIONS_MAX = 5
OWNER_QUESTIONS_HINT = (
    "If the owner's message answers one of them (a bare «yes», «no» or an option number counts), apply it "
    "in this turn: herdr_task decision (by:\"user\") with the answer in full words, clear that task's wait "
    "({reason:\"none\"}), send the answer to the agent pane, then confirm in one line. Otherwise ignore this block.")


def _owner_id() -> str:
    try:
        data = json.loads((PROFILE_HOME / "herdr-pipeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    owner = data.get("owner_user_id") if isinstance(data, dict) else None
    return owner if isinstance(owner, str) else ""


def open_questions_context(platform: str = "", sender_id: Any = "", **_: Any) -> dict[str, str] | None:
    """pre_llm_call: the owner's Telegram turn sees the questions agents wait on.

    Controller questions are delivered from webhook sessions the chat session never saw, so a bare
    «yes» from the owner once went unrouted and the task sat 11 h. Fail-open: any error injects nothing.
    """
    try:
        # The neighbour profile's scope of this plugin is loaded too; only the live home speaks.
        if platform != "telegram" or Path(get_hermes_home()).resolve() != PROFILE_HOME.resolve():
            return None
        owner = _owner_id()
        if not owner or str(sender_id or owner) != owner:
            return None
        tasks = json.loads(REGISTRY_PATH.read_text(encoding="utf-8")).get("tasks", [])
        lines = []
        for item in tasks:
            if not isinstance(item, dict) or not item.get("enabled", True) or item.get("completed"):
                continue
            wait = item.get("wait") if isinstance(item.get("wait"), dict) else {}
            if wait.get("reason") != "user_decision":
                continue
            note = str(wait.get("note") or "(question not recorded: read the task)")[:300]
            lines.append(f"- {item.get('id')} (pane {item.get('pane_id')}, since {item.get('updated_at', '')}): {note}")
    except Exception:
        return None
    if not lines:
        return None
    head = "Herdr: agents are waiting on the owner for these questions:"
    return {"context": "\n".join([head, *lines[:OWNER_QUESTIONS_MAX], OWNER_QUESTIONS_HINT])}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", open_questions_context)
    ctx.register_tool(name="herdr_inspect", toolset="herdr", schema=INSPECT_SCHEMA, handler=inspect, check_fn=_available, emoji="🐑")
    ctx.register_tool(name="herdr_control", toolset="herdr", schema=CONTROL_SCHEMA, handler=control, check_fn=_available, emoji="🐑")
    ctx.register_tool(name="herdr_destructive", toolset="herdr", schema=DESTRUCTIVE_SCHEMA, handler=destructive, check_fn=_available, emoji="⚠️")
    ctx.register_tool(name="herdr_task", toolset="herdr", schema=TASK_SCHEMA, handler=task, check_fn=_registry_available, emoji="📋")
    ctx.register_command("herdr", handler=_slash, description="Inspect Herdr sessions and agents.", args_hint="[status|sessions|workspaces|tabs|panes|agents] [session]")
