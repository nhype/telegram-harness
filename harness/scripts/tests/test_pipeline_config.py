import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import herdr_event_bridge as bridge

MP = {"profile": "demo", "project": "DemoApi", "cwd_prefixes": ["/srv/demo-api"],
      "sibling_prefix": True, "route": "herdr-agent-events", "chat_id": "111111111",
      "owner_user_id": "111111111", "approval_tag": "[DemoApi Herdr approval]"}
BASE = ["--registry", "/tmp/r.json", "--state", "/tmp/s.json"]


def write(tmp_path, data):
    path = tmp_path / "herdr-pipeline.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_load_valid_config(tmp_path):
    cfg = bridge.load_pipeline_config(write(tmp_path, MP))
    assert cfg["project"] == "DemoApi" and cfg["sibling_prefix"] is True
    assert cfg["chat_id"] == "111111111"


@pytest.mark.parametrize("broken", [
    "{not json", [], {**MP, "project": ""}, {k: v for k, v in MP.items() if k != "route"},
    {**MP, "cwd_prefixes": []}, {**MP, "cwd_prefixes": ["relative/path"]},
    {**MP, "cwd_prefixes": "/srv/demo-api"},
])
def test_invalid_config_is_rejected(tmp_path, broken):
    assert bridge.load_pipeline_config(write(tmp_path, broken)) is None


def test_missing_config_is_none(tmp_path):
    assert bridge.load_pipeline_config(tmp_path / "absent.json") is None


def test_config_supplies_route_defaults(tmp_path):
    args = bridge.parse_args(BASE + ["--config", str(write(tmp_path, MP))])
    assert (args.project, args.cwd_prefix, args.sibling_prefix, args.webhook) == (
        "DemoApi", ["/srv/demo-api"], True, "herdr-agent-events")


def test_explicit_flags_override_config(tmp_path):
    args = bridge.parse_args(BASE + ["--config", str(write(tmp_path, MP)), "--project", "Other",
                                     "--cwd-prefix", "/srv/other", "--webhook", "other-route"])
    assert (args.project, args.cwd_prefix, args.webhook) == ("Other", ["/srv/other"], "other-route")
    assert args.sibling_prefix is True


def test_without_config_defaults_are_unchanged():
    args = bridge.parse_args(BASE)
    assert (args.project, args.cwd_prefix, args.sibling_prefix, args.webhook) == (
        "", [], False, "herdr-agent-events")


def test_invalid_config_path_fails_loudly(tmp_path):
    with pytest.raises(SystemExit):
        bridge.parse_args(BASE + ["--config", str(write(tmp_path, "{broken"))])


def test_sibling_prefix_routes_demoapi_worktrees_only():
    prefixes = ["/srv/demo-api"]
    assert bridge.cwd_matches("/srv/demo-api-worktrees/pricing-cost-audit", prefixes, True)
    assert bridge.cwd_matches("/srv/demo-api/.integration/x", prefixes, True)
    assert bridge.cwd_matches("/srv/demo-api-worktrees/x (deleted)", prefixes, True)
    assert not bridge.cwd_matches("/srv/demo-apifoo", prefixes, True)
    assert not bridge.cwd_matches("/srv/example-app", prefixes, True)
