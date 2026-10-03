"""Each test runs against a scratch Hermes home, never the real one (Hermes refuses a live state.db).

Per test, not at import: other plugin suites collected in the same run set their own home.
"""
import pytest


@pytest.fixture(autouse=True)
def scratch_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / 'hermes-home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
