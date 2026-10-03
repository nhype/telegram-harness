import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import threading
import asyncio

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('HERMES_AGENT_DIR', os.path.expanduser('~/.hermes/hermes-agent')))
sys.path.insert(0, str(CORE))
# Patches Hermes gateway internals: runs only where Hermes Agent is installed.
for _dep in ('aiohttp', 'psutil', 'gateway.run_turn'):
    pytest.importorskip(_dep, reason='needs a Hermes Agent install (run with its venv python)')
from tools import approval
from tools.approval_gateway_wait import _await_gateway_decision
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.config import Platform


def load():
    path = ROOT / '__init__.py'
    assert path.exists(), 'ExampleApp approval bridge is not implemented'
    spec = importlib.util.spec_from_file_location('exampleapp_approval_bridge', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridge(monkeypatch):
    m = load()
    monkeypatch.setattr(m, 'active', lambda: True)
    monkeypatch.setattr(approval, '_gateway_queues', {})
    monkeypatch.setattr(approval, '_gateway_notify_cbs', {})
    return m.Bridge()


class Runner:
    def _profile_name_for_source(self, source):
        return 'exampleapp'

    def _is_user_authorized_for_source(self, source):
        return source.user_id == '111111111'

    def _session_key_for_source(self, source):
        return 'native-telegram'


def event(text='/approve', reply='91', chat='111111111', user='111111111', thread=None):
    return MessageEvent(text=text, source=SessionSource(platform=Platform.TELEGRAM,
        chat_id=chat, user_id=user, thread_id=thread), reply_to_message_id=reply)


def waiter(bridge, key='webhook-key', message='91'):
    ready = threading.Event()
    result = {}
    def notify(data):
        bridge.record(Runner.owner, key, data['request_id'], '111111111', None, message, 300)
        ready.set()
    def run():
        result.update(_await_gateway_decision(key, notify, {'command': 'test-only', 'pattern_key': key}))
    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(5)
    return thread, result


Runner.owner = Runner()


@pytest.mark.parametrize('case', ['ok', 'deny', 'failed_send', 'missing_message_id',
                                  'other_route', 'other_chat', 'insecure', 'coalescing',
                                  'other_profile', 'direct_spawn'])
def test_install_delivers_and_binds_only_authenticated_herdr(monkeypatch, case):
    m = load()
    assert hasattr(m, 'install'), 'Transport hooks not implemented'
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.run_turn_runner import TurnRunner
    from gateway.platforms.base import SendResult
    from gateway.config import PlatformConfig
    from tools import approval_context
    from contextlib import nullcontext
    import hmac, hashlib, json
    monkeypatch.setattr(m, 'active', lambda: True)
    monkeypatch.setattr(approval_context, '_get_approval_timeout', lambda: 5)
    monkeypatch.setattr(approval, '_gateway_queues', {})
    monkeypatch.setattr(approval, '_gateway_notify_cbs', {})
    m.install()
    async def scenario():
        route = {'secret': 'test-secret-not-a-credential', 'deliver': 'telegram',
                 'deliver_extra': {'chat_id': '111111111'}, 'prompt': 'test'}
        route_name = 'other-route' if case == 'other_route' else m.ROUTE
        if case == 'other_chat': route['deliver_extra']['chat_id'] = '999'
        if case == 'insecure': route['secret'] = 'INSECURE_NO_AUTH'
        if case == 'coalescing': route['coalesce'] = {'window_seconds': 1}
        if case == 'other_profile': monkeypatch.setattr(m, 'active', lambda: False)
        adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'routes': {route_name: route}}))
        if case == 'coalescing': monkeypatch.setattr(adapter._coalescer, 'enqueue', lambda **kw: False)
        runner = Runner()
        adapter.gateway_runner = runner
        sent = []
        class Transport:
            async def send(self, chat, text, metadata=None):
                sent.append((chat, text, metadata))
                return SendResult(success=case != 'failed_send',
                                  message_id=None if case == 'missing_message_id' else '91')
        monkeypatch.setattr(adapter, '_find_adapter', lambda *args: Transport())
        monkeypatch.setattr(adapter, '_profile_scope', lambda *args: nullcontext())
        captured = []
        async def capture(ev):
            captured.append(ev)
        monkeypatch.setattr(adapter, 'handle_message', capture)
        monkeypatch.setattr(adapter, '_resolve_route', lambda req: (route_name, route, None, None))
        body = json.dumps({'type': 'done'}).encode()
        class Request:
            content_length = len(body)
            method = 'POST'
            remote = '127.0.0.1'
            headers = {'X-Hub-Signature-256': 'sha256=' + hmac.new(route['secret'].encode(), body, hashlib.sha256).hexdigest(),
                       'X-Request-ID': 'test-delivery'}
            async def read(self): return body
        if case == 'direct_spawn':
            import time
            adapter._spawn_agent_run({}, 'test', 'direct', time.time(), route_config=route,
                route_name=route_name, profile=None, event_type='done')
        else:
            response = await adapter._handle_webhook(Request())
            assert response.status == 202
        await asyncio.sleep(0)
        assert len(captured) == 1
        source = captured[0].source
        ctx = NS(source=source, session_key='original-webhook-session', _status_adapter=adapter,
                 _status_chat_id=source.chat_id, _status_thread_metadata=None,
                 _loop_for_step=asyncio.get_running_loop())
        turn = TurnRunner(runner, ctx)
        if case in ('other_route', 'other_chat', 'insecure', 'coalescing', 'other_profile', 'direct_spawn'):
            assert m.eligible(turn) is None
            assert not m.BRIDGE.bindings
            return
        monkeypatch.setattr(turn, '_close_native_stream_boundary', lambda *a: None)
        monkeypatch.setattr(turn, '_schedule', lambda coro, *a: asyncio.run_coroutine_threadsafe(coro, ctx._loop_for_step))
        task = asyncio.create_task(asyncio.to_thread(_await_gateway_decision,
            ctx.session_key, turn._approval_notify_sync, {'command': 'harmless-test', 'description': 'test'}))
        for _ in range(1000):  # a cold first turn (fresh install) imports and compiles a lot
            if sent and m.BRIDGE.bindings: break
            await asyncio.sleep(.01)
        assert sent and m.BRIDGE.bindings
        if case in ('failed_send', 'missing_message_id'):
            result = await asyncio.wait_for(task, 3)
            assert result['notify_failed']
            assert not approval._gateway_queues
            assert 'Not approved' in m.BRIDGE.resolve(runner, event(), 'approve')
            return
        await asyncio.sleep(.02)
        assert approval._gateway_queues[ctx.session_key][0].settle is not None
        from gateway.slash_commands import GatewaySlashCommandsMixin
        handler = GatewaySlashCommandsMixin._handle_deny_command if case == 'deny' else GatewaySlashCommandsMixin._handle_approve_command
        answer = await handler(runner, event('/deny reason' if case == 'deny' else '/approve'))
        assert ('denied' if case == 'deny' else 'once') in answer
        result = await asyncio.wait_for(task, 3)
        assert result['choice'] == ('deny' if case == 'deny' else 'once')
        assert not approval._gateway_queues
        # The same core HMAC check rejects a forged event before it can spawn.
        Request.headers = {'X-Hub-Signature-256': 'sha256=bad', 'X-Request-ID': 'forged'}
        response = await adapter._handle_webhook(Request())
        assert response.status == 401
        assert len(captured) == 1
    try:
        asyncio.run(scenario())
    finally:
        m.uninstall()


@pytest.mark.parametrize('kwargs', [
    {'user': 'attacker'}, {'chat': '999'}, {'thread': '8'}, {'reply': 'unknown'},
    {'text': '/approve always'}, {'text': '/approve session'}, {'text': '/approve all'},
])
def test_scope_and_modes_cannot_resolve(bridge, kwargs):
    thread, result = waiter(bridge)
    try:
        bridge.resolve(Runner.owner, event(**kwargs), 'approve')
        assert not result
        assert thread.is_alive()
    finally:
        approval.unregister_gateway_notify('webhook-key')
        thread.join(3)


def test_expired_and_stale_reply_cannot_target_new_request(bridge):
    thread, result = waiter(bridge)
    try:
        bridge.bindings[0].deadline = 0
        assert 'Not approved' in bridge.resolve(Runner.owner, event(), 'approve')
        assert not result
    finally:
        approval.unregister_gateway_notify('webhook-key')
        thread.join(3)
    second, result2 = waiter(bridge, key='new', message='92')
    try:
        assert 'Not approved' in bridge.resolve(Runner.owner, event(), 'approve')
        assert not result2
    finally:
        approval.unregister_gateway_notify('new')
        second.join(3)


def test_concurrent_bare_fails_closed_reply_resolves_exact(bridge):
    first, a = waiter(bridge)
    second, b = waiter(bridge, key='second', message='92')
    try:
        assert 'Not approved' in bridge.resolve(Runner.owner, event(reply=None), 'approve')
        assert not a and not b
        assert 'denied' in bridge.resolve(Runner.owner, event('/deny no thanks', '92'), 'deny')
        second.join(3)
        assert b['choice'] == 'deny' and b['reason'] == 'no thanks'
        assert not a
        bridge.resolve(Runner.owner, event(reply=None), 'approve')
        first.join(3)
        assert a['choice'] == 'once'
    finally:
        for key in ('webhook-key', 'second'): approval.unregister_gateway_notify(key)
        first.join(3); second.join(3)


def test_inflight_delivery_makes_bare_ambiguous(bridge):
    from tools.approval_gateway_wait import _ApprovalEntry
    first, a = waiter(bridge)
    entry = _ApprovalEntry({'command': 'other'})
    approval._gateway_queues['other'] = [entry]
    bridge.record(Runner.owner, 'other', entry.data['request_id'], '111111111', None, None, 300)
    try:
        assert 'Not approved' in bridge.resolve(Runner.owner, event(reply=None), 'approve')
        assert not a
    finally:
        approval.unregister_gateway_notify('webhook-key')
        approval.unregister_gateway_notify('other')
        first.join(3)


@pytest.mark.parametrize('reply', [None, 'native-message'])
def test_native_telegram_is_not_intercepted_after_bridge_settled(bridge, reply):
    from tools.approval_gateway_wait import _ApprovalEntry
    bridge.record(Runner.owner, 'old', 'gone', '111111111', None, '90', 300)
    entry = _ApprovalEntry({'command': 'native'})
    approval._gateway_queues['native-telegram'] = [entry]
    assert bridge.resolve(Runner.owner, event(reply=reply), 'approve') is None
    assert entry.result is None


def test_prompt_from_previous_process_cannot_approve_native(bridge):
    from tools.approval_gateway_wait import _ApprovalEntry
    entry = _ApprovalEntry({'command': 'native'})
    approval._gateway_queues['native-telegram'] = [entry]
    ev = event()
    ev.reply_to_text = '[ExampleApp Herdr approval]\nOld request'
    assert 'Not approved' in bridge.resolve(Runner.owner, ev, 'approve')
    assert entry.result is None


def test_old_expired_reply_cannot_fall_through_to_native(bridge):
    from tools.approval_gateway_wait import _ApprovalEntry
    bridge.record(Runner.owner, 'old', 'old-request', '111111111', None, '90', 1)
    bridge.bindings[0].deadline = 0
    bridge.record(Runner.owner, 'new-settled', 'new-request', '111111111', None, '91', 1)
    entry = _ApprovalEntry({'command': 'native'})
    approval._gateway_queues['native-telegram'] = [entry]
    assert 'Not approved' in bridge.resolve(Runner.owner, event(reply='90'), 'approve')
    assert entry.result is None


def test_real_timeout_removes_core_request_and_rejects_late_reply(bridge, monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, '_get_approval_timeout', lambda: .03)
    thread, result = waiter(bridge)
    thread.join(3)
    assert not thread.is_alive()
    assert result['resolved'] is False
    assert not approval._gateway_queues
    assert 'Not approved' in bridge.resolve(Runner.owner, event(), 'approve')


@pytest.mark.parametrize('cancelled', [None, 'withdrawn'])
def test_request_id_collision_fails_closed(bridge, cancelled):
    from tools.approval_gateway_wait import _ApprovalEntry
    one = _ApprovalEntry({'request_id': 'collision'})
    two = _ApprovalEntry({'request_id': 'collision'})
    two.cancelled = cancelled
    approval._gateway_queues['collision-session'] = [one, two]
    bridge.record(Runner.owner, 'collision-session', 'collision', '111111111', None, '91', 300)
    assert 'Not approved' in bridge.resolve(Runner.owner, event(), 'approve')
    assert one.result is None and two.result is None


@pytest.mark.parametrize('case', ['authorization_revoked', 'bot', 'other_profile', 'control_disabled'])
def test_authorization_still_required(bridge, case, monkeypatch):
    thread, result = waiter(bridge)
    ev = event()
    if case == 'authorization_revoked':
        monkeypatch.setattr(Runner.owner, '_is_user_authorized_for_source', lambda source: False)
    if case == 'bot': ev.source.is_bot = True
    if case == 'other_profile': ev.source.profile = 'unrelated'
    if case == 'control_disabled': ev.allow_gateway_control = False
    try:
        assert 'Not approved' in bridge.resolve(Runner.owner, ev, 'approve')
        assert not result
    finally:
        approval.unregister_gateway_notify('webhook-key')
        thread.join(3)


def test_native_telegram_real_handler_with_plugin_installed(monkeypatch):
    from gateway.slash_commands import GatewaySlashCommandsMixin as Mixin
    from tools.approval_gateway_wait import _ApprovalEntry
    m = load()
    monkeypatch.setattr(m, 'active', lambda: True)
    monkeypatch.setattr(approval, '_gateway_queues', {})
    class NativeRunner(Runner):
        _blocking_approval_or_stale = Mixin._blocking_approval_or_stale
        _pending_approvals = {}
        async def _deliver_approval_confirmation(self, event, text, action): return text
    runner = NativeRunner()
    m.install()
    m.install()  # idempotent
    try:
        entry = _ApprovalEntry({'command': 'native-test'})
        approval._gateway_queues['native-telegram'] = [entry]
        asyncio.run(Mixin._handle_approve_command(runner, event('/approve session', reply=None)))
        assert entry.result == 'session'  # core native behavior intentionally unchanged
    finally:
        m.uninstall()


def test_reply_resolves_real_blocking_wait_preserving_source_key(bridge):
    thread, result = waiter(bridge)
    try:
        assert 'webhook-key' in approval._gateway_queues
        assert 'native-telegram' not in approval._gateway_queues
        answer = bridge.resolve(Runner.owner, event(), 'approve')
        assert 'once' in answer
        thread.join(3)
        assert not thread.is_alive()
        assert result['choice'] == 'once'
    finally:
        approval.unregister_gateway_notify('webhook-key')
        thread.join(3)


def test_own_route_chat_accepts_legacy_and_v2_ids():
    # Hermes 0.21.5+ keys webhook sessions as webhook:v2:<b64 [profile, route, delivery]>.
    sys.path.insert(0, str(ROOT.parents[1] / 'scripts'))
    import webhook_ids
    m = load()
    legacy, v2 = webhook_ids.session_chat_ids('herdr-agent-events', 'd1', 'exampleapp')
    assert m.own_route_chat(legacy) and m.own_route_chat(v2)
    assert m.own_route_chat(webhook_ids.session_chat_ids('herdr-agent-events', 'd1', None)[1])
    assert not m.own_route_chat(webhook_ids.session_chat_ids('herdr-agent-events', 'd1', 'other')[1])
    assert not m.own_route_chat(webhook_ids.session_chat_ids('herdr-other', 'd1', 'exampleapp')[1])
    assert not m.own_route_chat('111111111')


def test_settings_come_from_profile_config():
    m = load()
    assert (m.PROFILE, m.ROUTE, m.CHAT, m.OWNER, m.PROMPT_TAG) == (
        'exampleapp', 'herdr-agent-events', '111111111', '111111111', '[ExampleApp Herdr approval]')
    assert m.active()


def test_incomplete_config_disables_bridge(tmp_path, monkeypatch):
    (tmp_path / 'herdr-pipeline.json').write_text(
        '{"profile": "demo", "route": "herdr-agent-events", "chat_id": "111111111"}')
    monkeypatch.setattr('hermes_constants.get_hermes_home', lambda: tmp_path)
    m = load()
    assert m.PROFILE_HOME is None and m.CHAT == '' and not m.active()


def load_as(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('order', [('exampleapp', 'demo'), ('demo', 'exampleapp')])
def test_each_profile_scope_installs_its_own_hooks(tmp_path, monkeypatch, order):
    """Hermes loads a neighbour profile's plugins into the same gateway process (second scope)."""
    mods = []
    for profile in order:
        home = tmp_path / profile
        home.mkdir()
        (home / 'herdr-pipeline.json').write_text(json.dumps({
            'profile': profile, 'route': 'herdr-agent-events', 'chat_id': '111111111',
            'owner_user_id': '111111111', 'approval_tag': f'[{profile} Herdr approval]'}))
        monkeypatch.setattr('hermes_constants.get_hermes_home', lambda home=home: home)
        mods.append(load_as(f'approval_scope_{profile}_{order[0]}'))
    try:
        for m in mods:
            m.install()
        assert [bool(m._PATCHES) for m in mods] == [True, True]
    finally:
        for m in reversed(mods):
            m.uninstall()
