"""The controller run's session is closed in the routed profile's scope (multiplexed host gateway)."""
import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

for _dep in ('aiohttp', 'psutil', 'gateway.run_turn'):
    pytest.importorskip(_dep, reason='needs a Hermes Agent install (run with its venv python)')
from gateway.platforms.webhook import WebhookAdapter  # noqa: E402

PLUGIN = Path(__file__).parents[1] / '__init__.py'


def load(home, monkeypatch, name):
    (home / 'herdr-pipeline.json').write_text(json.dumps({
        'profile': home.name, 'project': 'Acme', 'cwd_prefixes': ['/srv/acme'],
        'route': f'herdr-{home.name}'}))
    monkeypatch.setattr('hermes_constants.get_hermes_home', lambda: home)
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def adapter_calls(monkeypatch, tmp_path):
    """Original on_processing_complete records the scope it ran in; scopes are recorded too."""
    seen, scope = [], {'profile': None}

    async def original(self, event, outcome):
        seen.append(scope['profile'])

    @contextlib.contextmanager
    def fake_scope(profile):
        scope['profile'] = profile
        try:
            yield
        finally:
            scope['profile'] = None

    monkeypatch.setattr(WebhookAdapter, 'on_processing_complete', original)
    monkeypatch.setattr(WebhookAdapter, '_profile_scope', staticmethod(fake_scope))
    return seen


import sys  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
import webhook_ids  # noqa: E402


def event(profile, route, fmt='v2'):
    legacy, v2 = webhook_ids.session_chat_ids(route, 'd1', profile)
    return NS(source=NS(profile=profile, chat_id=v2 if fmt == 'v2' else legacy, user_id=f'webhook:{route}'))


def run(evt):
    asyncio.run(WebhookAdapter.on_processing_complete(object(), evt, 'success'))


def test_own_route_closes_inside_the_profile_scope(tmp_path, monkeypatch, adapter_calls):
    home = tmp_path / 'acme'
    home.mkdir()
    plugin = load(home, monkeypatch, 'sc_acme')
    monkeypatch.setattr(plugin, 'get_hermes_home', lambda: tmp_path)  # called from the launch scope
    plugin.install_session_close()
    run(event('acme', 'herdr-acme'))
    run(event('acme', 'herdr-acme', fmt='legacy'))
    assert adapter_calls == ['acme', 'acme']


def test_other_routes_are_left_alone(tmp_path, monkeypatch, adapter_calls):
    home = tmp_path / 'acme'
    home.mkdir()
    plugin = load(home, monkeypatch, 'sc_acme2')
    monkeypatch.setattr(plugin, 'get_hermes_home', lambda: tmp_path)
    plugin.install_session_close()
    run(event('other', 'herdr-other'))
    run(event(None, 'github'))
    assert adapter_calls == [None, None]


def test_two_profiles_each_scope_their_own_route(tmp_path, monkeypatch, adapter_calls):
    for name in ('acme', 'beta'):
        home = tmp_path / name
        home.mkdir()
        plugin = load(home, monkeypatch, f'sc_{name}')
        monkeypatch.setattr(plugin, 'get_hermes_home', lambda: tmp_path)
        plugin.install_session_close()
    run(event('beta', 'herdr-beta'))
    run(event('acme', 'herdr-acme'))
    assert adapter_calls == ['beta', 'acme']
