"""Profile-scoped, request-ID-bound bridge for authenticated Herdr approvals.

No queue is re-keyed; core remains the sole decision committer. No disk state.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time

from hermes_constants import get_hermes_home

_REQUIRED = ('profile', 'route', 'chat_id', 'owner_user_id', 'approval_tag')


def _settings():
    """Routing from <HERMES_HOME>/herdr-pipeline.json; None disables the bridge.

    The chat/owner pair turns a chat into a permission-granting surface, so it is
    never defaulted: it must be written explicitly in the (root-only) profile file.
    """
    home = Path(get_hermes_home()).resolve()
    try:
        data = json.loads((home / 'herdr-pipeline.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in _REQUIRED):
        return None
    return home, data


_SETTINGS = _settings()
PROFILE_HOME = _SETTINGS[0] if _SETTINGS else None
_CFG = _SETTINGS[1] if _SETTINGS else {}
PROFILE = _CFG.get('profile', '')
ROUTE = _CFG.get('route', '')
CHAT = _CFG.get('chat_id', '')
OWNER = _CFG.get('owner_user_id', '')
PROMPT_TAG = _CFG.get('approval_tag', '')
REFUSE = 'Not approved: the request expired, is unavailable or ambiguous. Reply to the specific request message.'


def active():
    return PROFILE_HOME is not None and Path(get_hermes_home()).resolve() == PROFILE_HOME


def norm(value):
    return str(value) if value not in (None, '') else None


@dataclass
class Binding:
    owner: object
    session: str
    request: str
    chat: str
    thread: str | None
    message: str | None
    deadline: float


class Bridge:
    def __init__(self):
        self.bindings = []
        self.lock = threading.RLock()

    def record(self, owner, session, request, chat, thread, message, timeout):
        if not request or str(chat) != CHAT:
            return
        with self.lock:
            # Keep tombstones for this process's lifetime: a stale reply must
            # never fall through to a newly pending native Telegram approval.
            # Core owns queue lifetime; these small records cannot resurrect it.
            now = time.monotonic()
            for b in self.bindings:
                if b.owner is owner and b.session == session and b.request == request:
                    if message:
                        b.message = str(message)
                    return
            self.bindings.append(Binding(owner, session, request, str(chat), norm(thread),
                                         norm(message), now + max(0, timeout)))

    def resolve(self, runner, event, verb):
        from tools import approval
        source = event.source
        if not active() or getattr(source.platform, 'value', source.platform) != 'telegram':
            return None
        with self.lock:
            bindings = [b for b in self.bindings if b.owner is runner]
            reply = norm(event.reply_to_message_id)
            exact = [b for b in bindings if b.message == reply and b.chat == str(source.chat_id)]
            scoped = [b for b in bindings if b.chat == str(source.chat_id)
                      and b.thread == norm(source.thread_id)]
            # Quoted text is NEVER approval authority. The tag can only deny
            # an unbound replay (e.g. a prompt from a previous gateway process).
            if reply and not exact and PROMPT_TAG in (event.reply_to_text or ''):
                return REFUSE
            if not exact and not scoped:
                return None
            if (str(source.chat_id) != CHAT or str(source.user_id) != OWNER
                    or getattr(source, 'is_bot', False)
                    or getattr(source, 'profile', None) not in (None, '', PROFILE)
                    or not event.allow_gateway_control
                    or not runner._is_user_authorized_for_source(source)):
                return REFUSE
            if reply:
                candidates = [b for b in exact if b.thread == norm(source.thread_id)]
            else:
                candidates = scoped
            now = time.monotonic()
            with approval._lock:
                live = []
                for b in candidates:
                    matches = [e for e in approval._gateway_queues.get(b.session, [])
                               if e.data.get('request_id') == b.request]
                    if len(matches) != 1 or b.deadline <= now:
                        continue
                    entry = matches[0]
                    if not entry.event.is_set() and entry.result is None and not entry.cancelled:
                        live.append(b)
                native = bool(approval._gateway_queues.get(runner._session_key_for_source(source)))
                scoped_pending = any(e.data.get('request_id') == b.request
                    for b in scoped for e in approval._gateway_queues.get(b.session, []))
            if native and not scoped_pending and not (reply and exact):
                return None  # ordinary Telegram request, not a stale bridge reply
            if len(live) != 1 or (not reply and native) or not live[0].message:
                return REFUSE
            args = event.get_command_args().strip()
            if verb == 'approve' and args:
                return 'Herdr allows only /approve (once); session, always and all are not supported.'
            if verb == 'deny' and args.lower().split()[:1] == ['all']:
                return 'Reply /deny to a specific request; all is not supported.'
            b = live[0]
            count = approval.resolve_gateway_approval(b.session, 'once' if verb == 'approve' else 'deny',
                resolve_all=False, reason=args[:280] or None if verb == 'deny' else None, request_id=b.request)
            if count != 1:
                return REFUSE
            return 'Approved once.' if verb == 'approve' else 'Request denied.'


# Hooks are process-local, installed only by this profile's plugin loader.
from contextvars import ContextVar
from functools import wraps
import inspect
import logging

BRIDGE = Bridge()
_AUTH = ContextVar('herdr_approval_authenticated_ingress', default=None)
_PATCHES = []
# Per-home: a gateway may also load a neighbour profile's copy of this plugin
# (second scope); each scope installs its own wrappers, gated by active().
_MARKER = f'_herdr_approval_origin:{PROFILE_HOME}'


def destination(route):
    extra = route.get('deliver_extra') or {}
    chat = str(extra.get('chat_id', ''))
    thread = norm(extra.get('message_thread_id') or extra.get('thread_id'))
    if route.get('deliver') != 'telegram' or chat != CHAT:
        return None
    # Never make payload-templated routing an approval authority.
    if thread is not None and not thread.isdecimal():
        return None
    return chat, thread


def eligible(turn):
    if not active():
        return None
    ctx = turn._ctx
    source = ctx.source
    if getattr(source.platform, 'value', source.platform) != 'webhook':
        return None
    adapter = ctx._status_adapter
    delivery = getattr(adapter, '_delivery_info', {}).get(ctx._status_chat_id, {})
    origin = delivery.get(_MARKER)
    if (not origin or origin[0] is not turn._runner
            or source.user_id != 'webhook:' + ROUTE
            or not str(source.chat_id).startswith('webhook:' + ROUTE + ':')
            or ctx._status_chat_id != source.chat_id
            or destination(delivery) != origin[1]
            or not ctx.session_key):
        return None
    return origin[1]


def install():
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.run_turn_runner import TurnRunner
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from tools import approval
    if _PATCHES or getattr(TurnRunner._approval_notify_sync, _MARKER, False):
        return
    if 'request_id' not in inspect.signature(approval.resolve_gateway_approval).parameters:
        raise RuntimeError('Herdr approval bridge requires request-ID-aware core resolver')
    originals = {name: getattr(WebhookAdapter, name) for name in
                 ('_handle_webhook', '_read_authenticated_body', '_spawn_agent_run')}
    old_notify = TurnRunner._approval_notify_sync

    @wraps(originals['_handle_webhook'])
    async def ingress(self, request):
        token = _AUTH.set(None)
        try:
            return await originals['_handle_webhook'](self, request)
        finally:
            _AUTH.reset(token)

    @wraps(originals['_read_authenticated_body'])
    async def authenticated(self, request, route_name, route_config):
        result = await originals['_read_authenticated_body'](self, request, route_name, route_config)
        secret = route_config.get('secret', self._global_secret)
        if (result[1] is None and route_name == ROUTE and secret
                and secret != 'INSECURE_NO_AUTH' and not route_config.get('coalesce')):
            _AUTH.set((self, route_config))
        return result

    @wraps(originals['_spawn_agent_run'])
    def spawn(self, payload, prompt, delivery_id, now, *, route_config, route_name, profile, event_type):
        task = originals['_spawn_agent_run'](self, payload, prompt, delivery_id, now,
            route_config=route_config, route_name=route_name, profile=profile, event_type=event_type)
        auth = _AUTH.get()
        with self._profile_scope(profile):
            if (active() and route_name == ROUTE and auth is not None
                    and auth[0] is self and auth[1] is route_config
                    and (target := destination(route_config))):
                delivery = self._delivery_info.get(f'webhook:{route_name}:{delivery_id}', {})
                if destination(delivery) == target:
                    delivery[_MARKER] = (self.gateway_runner, target)
        return task

    @wraps(old_notify)
    def notify(self, data):
        target = eligible(self)
        if target is None:
            return old_notify(self, data)
        from gateway.run import _redact_approval_command, _format_exec_approval_fallback
        from tools.approval_context import _get_approval_timeout
        ctx = self._ctx
        adapter = ctx._status_adapter
        adapter.pause_typing_for_chat(ctx._status_chat_id)
        self._close_native_stream_boundary('Approval')
        content = PROMPT_TAG + '\n' + _format_exec_approval_fallback(
            _redact_approval_command(data.get('command', '')),
            data.get('description', 'dangerous command'), '/',
            allow_permanent=False, allow_session=False, smart_denied=data.get('smart_denied', False))
        content += '\n\nReply to this message: /approve — once; /deny [reason] — refuse. With several pending requests, a reply to the message is required.'

        async def deliver():
            # Recheck after the worker-to-loop hop. Use the normal webhook send
            # and its profile-scoped Telegram egress, not a raw bot API bypass.
            if eligible(self) != target:
                raise RuntimeError('Herdr approval destination changed')
            result = await adapter.send(ctx._status_chat_id, content, metadata={'is_approval_prompt': True})
            if not result.success or not result.message_id:
                raise RuntimeError('Herdr approval prompt delivery not confirmed')
            BRIDGE.record(self._runner, ctx.session_key, data.get('request_id'), *target,
                          result.message_id, _get_approval_timeout())
            return result

        BRIDGE.record(self._runner, ctx.session_key, data.get('request_id'), *target,
                      None, _get_approval_timeout())
        future = self._schedule(deliver(), 'Herdr approval delivery scheduling failed')
        if future is None:
            raise RuntimeError('Herdr approval event loop unavailable')
        # A timeout raises to the core notify_failed cleanup. A late delivery
        # cannot approve anything: its request ID is no longer in the core queue.
        future.result(timeout=15)
        from gateway.run_turn_runner_approval_settle import register_timeout_notice
        register_timeout_notice(self, data,
            command=_redact_approval_command(data.get('command', '')), card_message_id=None)

    def command_wrapper(original, verb):
        @wraps(original)
        async def command(self, event):
            result = BRIDGE.resolve(self, event, verb)
            if result is not None:
                return result
            return await original(self, event)
        return command

    changes = [(WebhookAdapter, '_handle_webhook', ingress),
               (WebhookAdapter, '_read_authenticated_body', authenticated),
               (WebhookAdapter, '_spawn_agent_run', spawn),
               (TurnRunner, '_approval_notify_sync', notify)]
    for verb in ('approve', 'deny'):
        name = f'_handle_{verb}_command'
        changes.append((GatewaySlashCommandsMixin, name,
                        command_wrapper(getattr(GatewaySlashCommandsMixin, name), verb)))
    for cls, name, replacement in changes:
        original = getattr(cls, name)
        setattr(replacement, _MARKER, True)
        _PATCHES.append((cls, name, original, replacement))
        setattr(cls, name, replacement)
    logging.getLogger(__name__).info('Herdr request-bound approval bridge installed (profile=%s)', PROFILE)


def uninstall():
    """Test/controlled rollback only; never called while the gateway has waits."""
    for cls, name, original, replacement in reversed(_PATCHES):
        if getattr(cls, name) is replacement:
            setattr(cls, name, original)
    _PATCHES.clear()
    BRIDGE.bindings.clear()


def register(ctx):
    if active():
        install()
