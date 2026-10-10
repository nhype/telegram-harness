"""Templates stay consistent with the code that consumes them."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness" / "scripts"))
from herdr_event_bridge import load_pipeline_config  # noqa: E402

UNIT_TOKENS = {"@PROFILE@", "@GATEWAY_UNIT@", "@USER_LINE@", "@HOME@", "@HOST_HOME@", "@PROFILE_HOME@",
               "@PATH@", "@PYTHON@", "@ROOT@", "@SOCKET@", "@HERMES_BIN@", "@HERDR_BIN@", "@WANTED_BY@"}


def test_pipeline_example_is_valid():
    assert load_pipeline_config(ROOT / "templates" / "herdr-pipeline.json.example") is not None


def test_unit_templates_use_only_known_tokens():
    for unit in (ROOT / "templates" / "systemd").glob("*.in"):
        used = set(re.findall(r"@[A-Z_]+@", unit.read_text()))
        assert used <= UNIT_TOKENS, (unit.name, used - UNIT_TOKENS)


def test_herdr_server_unit_sets_path_for_agent_shells():
    # Pane shells inherit the server's environment; without PATH `claude` may not be found.
    text = (ROOT / "templates" / "systemd" / "herdr-server.service.in").read_text()
    assert "Environment=PATH=@PATH@" in text


def test_webhook_prompt_names_the_controller_skill_and_project_placeholder():
    text = (ROOT / "templates" / "webhook-prompt.txt").read_text()
    assert "harness-controller" in text and "{{PROJECT}}" in text and "[SILENT]" in text


def test_claude_settings_is_json():
    assert isinstance(json.loads((ROOT / "templates" / "claude-settings.json").read_text()), dict)


def test_repo_settings_also_compact_early_whatever_the_launch_command():
    # The controller sometimes copies a launch command from the repo docs without `--settings`.
    settings = json.loads((ROOT / "templates" / "claude-settings.json").read_text())
    assert settings["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "400000"
    assert settings["promptSuggestionEnabled"] is False


def test_agent_launch_settings_compact_early_and_disable_prompt_suggestions():
    settings = json.loads((ROOT / "templates" / "claude-agent-settings.json").read_text())
    assert settings["promptSuggestionEnabled"] is False
    assert int(settings["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]) > 0


def test_delivery_skill_launches_agents_with_the_settings_file():
    text = (ROOT / "skills" / "harness-delivery" / "SKILL.md").read_text()
    assert re.search(r"claude --dangerously-skip-permissions --settings\s+\{\{AGENT_SETTINGS\}\}", text)
