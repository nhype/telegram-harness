"""Two profiles import the same plugin code; each must keep its own state."""
import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).parents[1] / "__init__.py"
CORE = Path(__file__).resolve().parents[3]


def load(name, path=PLUGIN):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_profile_gets_its_own_state(tmp_path, monkeypatch):
    a, b = tmp_path / "exampleapp", tmp_path / "demo"
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: a)
    ma = load("iso_a")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: b)
    mb = load("iso_b")
    assert ma.REGISTRY_PATH == a / "state" / "herdr_tasks.json"
    assert mb.REGISTRY_PATH == b / "state" / "herdr_tasks.json"
    assert ma.SENDS_PATH.parent == a / "state" and mb.SENDS_PATH.parent == b / "state"
    assert ma.SCRIPTS_DIR == mb.SCRIPTS_DIR == CORE / "scripts"


def test_symlinked_plugin_uses_shared_scripts_and_profile_state(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    (profile / "plugins").mkdir(parents=True)
    (profile / "plugins" / "herdr-control").symlink_to(PLUGIN.parent)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: profile)
    m = load("iso_link", profile / "plugins" / "herdr-control" / "__init__.py")
    assert m.SCRIPTS_DIR == CORE / "scripts"
    assert m.REGISTRY_PATH == profile / "state" / "herdr_tasks.json"
