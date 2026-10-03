from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1] / "__init__.py"
spec = importlib.util.spec_from_file_location("herdr_control_under_test", PLUGIN)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def decode(value: str) -> dict:
    return json.loads(value)


def test_sensitive_command_text_is_redacted() -> None:
    argv = [module.HERDR, "--session", "default", "agent", "prompt", "reviewer", "private prompt"]
    safe = module._safe_command(argv)
    assert "private prompt" not in safe
    assert safe[-1] == "<redacted:14 chars>"


@pytest.mark.parametrize("session", ["agent", "pane"])
def test_sensitive_redaction_cannot_be_bypassed_by_session_name(session: str) -> None:
    argv = [module.HERDR, "--session", session, "agent", "prompt", "reviewer", "private prompt"]
    safe = module._safe_command(argv)
    assert "private prompt" not in safe
    assert safe[-1] == "<redacted:14 chars>"


def test_timeout_partial_bytes_are_json_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=0.1, output=b"partial utf8: \xd0\xbe\xd0\xba\n")

    monkeypatch.setattr(module.subprocess, "run", timeout)
    result = module._run([module.HERDR, "--session", "default", "status"], 100)
    assert result["success"] is False
    assert result["error"] == "bridge_timeout"
    assert result["output"] == "partial utf8: ок\n"
    json.dumps(result, ensure_ascii=False)


def test_wait_output_match_is_redacted() -> None:
    argv = [
        module.HERDR, "--session", "pane", "pane", "wait-output", "w1:p1",
        "--match", "private marker", "--timeout", "1000",
    ]
    safe = module._safe_command(argv)
    assert "private marker" not in safe
    assert safe[7] == "<redacted:14 chars>"


def test_destructive_requires_exact_confirmation() -> None:
    result = decode(module.destructive({
        "action": "pane_close", "session": "default", "target": "w1:p1", "confirm": False,
    }))
    assert result["success"] is False
    assert result["error"] == "validation_error"


def test_unsafe_key_is_rejected_before_subprocess() -> None:
    result = decode(module.control({
        "action": "agent_send_keys", "session": "default", "target": "reviewer",
        "keys": ["ctrl+alt+delete"],
    }))
    assert result["success"] is False
    assert result["error"] == "validation_error"


@pytest.mark.parametrize("session", ["", "../default", "a b", "x/../../y", "x\nstatus"])
def test_invalid_session_names_are_rejected(session: str) -> None:
    if session == "":
        assert module._clean_session(session) == "default"
    else:
        with pytest.raises(ValueError):
            module._clean_session(session)


def test_relative_or_missing_cwd_is_rejected() -> None:
    with pytest.raises(ValueError):
        module._cwd("relative/path")
    with pytest.raises(ValueError):
        module._cwd("/definitely/not/a/real/herdr/bridge/path")


def test_live_status_read_only() -> None:
    socket = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr" / "herdr.sock"
    if not module._available() or not socket.exists():
        pytest.skip("needs a running Herdr server")
    result = decode(module.inspect({"action": "status", "session": "default"}))
    assert result["success"] is True
    assert result["exit_code"] == 0
