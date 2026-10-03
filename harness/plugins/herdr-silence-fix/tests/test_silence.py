import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock
import os
import pytest

# Patches Hermes gateway internals: runs only where Hermes Agent is installed.
for _dep in ('aiohttp', 'psutil', 'gateway.run_turn'):
    pytest.importorskip(_dep, reason='needs a Hermes Agent install (run with its venv python)')
import gateway.response_filters as filters
import gateway.run_turn as turn
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

ROOT = Path(os.environ.get('HERMES_AGENT_DIR', os.path.expanduser('~/.hermes/hermes-agent')))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
plugin=load('herdr_silence_fix',Path(__file__).parents[1]/'__init__.py')
try:  # Hermes's own test helpers need its full dev environment
    base=load('silence_test_helpers',ROOT/'tests/gateway/test_gateway_silence_tokens.py')
except (ImportError, OSError) as exc:
    pytest.skip(f'needs the Hermes Agent checkout and venv: {exc}', allow_module_level=True)

@pytest.fixture
def installed(monkeypatch, tmp_path):
    monkeypatch.setattr(filters,'display_kind_for_event',filters.display_kind_for_event)
    monkeypatch.setattr(turn,'display_kind_for_event',turn.display_kind_for_event)
    monkeypatch.setattr(plugin,'PROFILE_HOME',tmp_path.resolve())
    monkeypatch.setattr(plugin,'ROUTE_USER','webhook:herdr-agent-events')
    monkeypatch.setattr(plugin,'get_hermes_home',lambda: tmp_path)
    if not os.getenv('TEST_WITHOUT_SILENCE_FIX'):
        plugin.install()

def event(platform=Platform.WEBHOOK, route='herdr-agent-events'):
    source=SessionSource(platform=platform,chat_id=f'webhook:{route}:test',chat_type='dm',user_id=f'webhook:{route}')
    return MessageEvent(text='background lifecycle event',source=source,message_id='test')

@pytest.mark.parametrize('token',['[SILENT]','NO_REPLY'])
@pytest.mark.asyncio
async def test_real_gateway_herdr_silence(installed,monkeypatch,tmp_path,token):
    runner=base._runner(monkeypatch,tmp_path)
    runner._run_agent=AsyncMock(return_value={'final_response':token,'messages':[], 'tools':[], 'history_offset':0,'last_prompt_tokens':0,'api_calls':1,'failed':False})
    ev=event()
    result=await runner._handle_message_with_agent(ev,ev.source,'agent:main:webhook:test',1)
    assert result == ''
    assert not ev.internal

@pytest.mark.asyncio
async def test_human_warning_preserved(installed,monkeypatch,tmp_path):
    helper = base.test_human_turn_gets_a_visible_fallback_for_a_silence_marker
    import inspect
    if 'reply_expected' in inspect.signature(helper).parameters:  # Hermes 0.21.5+
        for reply_expected in (None, True):
            await helper(monkeypatch, tmp_path, reply_expected)
    else:
        await helper(monkeypatch, tmp_path)

@pytest.mark.asyncio
async def test_real_notification_preserved(installed,monkeypatch,tmp_path):
    runner=base._runner(monkeypatch,tmp_path)
    runner._run_agent=AsyncMock(return_value={'final_response':'Нужен ключ для продолжения.','messages':[], 'tools':[], 'history_offset':0,'last_prompt_tokens':0,'api_calls':1,'failed':False})
    ev=event()
    result=await runner._handle_message_with_agent(ev,ev.source,'agent:main:webhook:test',1)
    assert 'Нужен ключ' in result

def test_other_routes_profiles_and_forged_text_unchanged(installed,monkeypatch):
    assert filters.display_kind_for_event(event(route='other')) is None
    assert filters.display_kind_for_event(event(platform=Platform.TELEGRAM)) is None
    monkeypatch.setattr(plugin,'get_hermes_home',lambda: Path('/non-exampleapp'))
    assert filters.display_kind_for_event(event()) is None

def test_v2_chat_ids_of_own_route_are_machinery(installed, monkeypatch):
    # Hermes 0.21.5+ keys webhook sessions as webhook:v2:<b64 [profile, route, delivery]>.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
    import webhook_ids
    monkeypatch.setattr(plugin, 'PROFILE', 'exampleapp')

    def v2_event(profile):
        chat = webhook_ids.session_chat_ids('herdr-agent-events', 'd1', profile)[1]
        source = SessionSource(platform=Platform.WEBHOOK, chat_id=chat, chat_type='dm',
                               user_id='webhook:herdr-agent-events')
        return MessageEvent(text='background lifecycle event', source=source, message_id='d1')

    for profile in ('exampleapp', 'default'):
        assert filters.display_kind_for_event(v2_event(profile)) == filters.INTERNAL_NOTIFICATION_DISPLAY_KIND
    assert filters.display_kind_for_event(v2_event('someone-else')) is None


def test_failed_result_not_silenced(installed):
    assert not filters.is_intentional_silence_agent_result({'failed':True},'[SILENT]')

def test_idempotent_install_and_alias(installed):
    plugin.install(); original=filters.display_kind_for_event;plugin.install()
    assert original is filters.display_kind_for_event
    assert turn.display_kind_for_event is original

def test_settings_read_route_from_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin,'get_hermes_home',lambda: tmp_path)
    assert plugin._settings() == (None, '', '')
    (tmp_path/'herdr-pipeline.json').write_text('{"route": "herdr-acme", "profile": "acme"}')
    assert plugin._settings() == (tmp_path.resolve(), 'herdr-acme', 'acme')

@pytest.mark.parametrize('order',[('a','b'),('b','a')])
def test_both_profile_scopes_classify_their_own_turns(tmp_path,monkeypatch,order):
    # Hermes loads a neighbour profile's plugins into the same gateway process (second scope).
    monkeypatch.setattr(filters,'display_kind_for_event',filters.display_kind_for_event)
    monkeypatch.setattr(turn,'display_kind_for_event',turn.display_kind_for_event)
    current,mods={},{}
    for key in order:
        home=tmp_path/key;home.mkdir()
        (home/'herdr-pipeline.json').write_text('{"route": "herdr-agent-events"}')
        monkeypatch.setattr('hermes_constants.get_hermes_home',lambda home=home: home)
        mods[key]=load(f'silence_scope_{key}_{order[0]}',Path(__file__).parents[1]/'__init__.py')
        mods[key].get_hermes_home=lambda: current['home']
    for key in order:
        mods[key].install()
    for key in ('a','b'):
        current['home']=tmp_path/key
        assert filters.display_kind_for_event(event())==filters.INTERNAL_NOTIFICATION_DISPLAY_KIND,key

def test_unconfigured_profile_installs_nothing(tmp_path,monkeypatch):
    monkeypatch.setattr(filters,'display_kind_for_event',filters.display_kind_for_event)
    monkeypatch.setattr(turn,'display_kind_for_event',turn.display_kind_for_event)
    monkeypatch.setattr('hermes_constants.get_hermes_home',lambda: tmp_path)
    m=load('silence_unconfigured',Path(__file__).parents[1]/'__init__.py')
    original=filters.display_kind_for_event
    m.install()
    assert filters.display_kind_for_event is original
