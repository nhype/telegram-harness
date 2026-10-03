"""Profile-local compatibility patch for Herdr webhook silence.

Only delivery/presentation classification changes; event.internal is NOT changed.
Remove once Hermes natively classifies autonomous webhook turns for silence.
"""
from functools import wraps
import importlib.util
import json
import logging
from pathlib import Path
import sys

from hermes_constants import get_hermes_home


def _load_webhook_ids():
    """harness/scripts/webhook_ids.py, next to this plugin's code (it is symlinked into profiles)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "webhook_ids.py"
    spec = importlib.util.spec_from_file_location("herdr_harness_webhook_ids", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


webhook_ids = _load_webhook_ids()


def _settings():
    """(profile home, route, profile) from <HERMES_HOME>/herdr-pipeline.json; (None, '', '') if unset."""
    home = Path(get_hermes_home()).resolve()
    try:
        data = json.loads((home / "herdr-pipeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "", ""
    if not isinstance(data, dict):
        return None, "", ""
    route, profile = data.get("route"), data.get("profile")
    if not isinstance(route, str) or not route:
        return None, "", ""
    return home, route, profile if isinstance(profile, str) else ""


PROFILE_HOME, _ROUTE, PROFILE = _settings()
ROUTE_USER = f"webhook:{_ROUTE}" if _ROUTE else ""


def own_route_chat(chat_id) -> bool:
    """True for a session chat id of this profile's route (legacy or Hermes 0.21.5+ v2 format).

    A v2 id names the route's profile: this profile when the host gateway routes to it, or
    "default" on a gateway of the profile's own.
    """
    route = ROUTE_USER[len("webhook:"):] if ROUTE_USER.startswith("webhook:") else ""
    parsed = webhook_ids.parse(chat_id)
    return bool(route) and parsed is not None and parsed[1] == route and parsed[0] in (None, "default", PROFILE)


def install():
    from gateway import response_filters

    if PROFILE_HOME is None:
        return  # this profile has no pipeline config
    # Per-home: a gateway may also load a neighbour profile's copy of this plugin
    # (second scope); each scope wraps once and only classifies its own home's turns.
    marker = f"_herdr_silence_fix:{PROFILE_HOME}"
    original = response_filters.display_kind_for_event
    if getattr(original, marker, False):
        return

    @wraps(original)
    def classify(event):
        kind = original(event)
        if kind is not None:
            return kind
        source = getattr(event, "source", None)
        platform = getattr(source, "platform", None)
        platform = getattr(platform, "value", platform)
        if (
            PROFILE_HOME is not None and Path(get_hermes_home()).resolve() == PROFILE_HOME
            and platform == "webhook"
            and getattr(source, "user_id", None) == ROUTE_USER
            and own_route_chat(getattr(source, "chat_id", ""))
        ):
            return response_filters.INTERNAL_NOTIFICATION_DISPLAY_KIND
        return kind

    setattr(classify, marker, True)
    response_filters.display_kind_for_event = classify
    # run_turn imports the helper by value. Cover both plugin load orders.
    turn = sys.modules.get("gateway.run_turn")
    if turn is not None and getattr(turn, "display_kind_for_event", None) is original:
        turn.display_kind_for_event = classify
    logging.getLogger(__name__).info("Herdr silence compatibility fix installed")


def install_session_close():
    """Close this profile's controller sessions in the profile's own scope.

    On a multiplexed host gateway the adapter calls ``on_processing_complete`` after the run has
    left the routed profile's scope, so Hermes looks the per-delivery session up in the host's
    store and never closes the profile's row. The bridge waits for that close to release the
    workflow, and would hold every task until its 30-minute cap. Remove once Hermes closes
    routed webhook sessions in the route's profile scope.
    """
    from gateway.platforms.webhook import WebhookAdapter

    if PROFILE_HOME is None or not PROFILE:
        return
    marker = f"_herdr_session_close:{PROFILE_HOME}"
    original = WebhookAdapter.on_processing_complete
    if getattr(original, marker, False):
        return

    @wraps(original)
    async def on_processing_complete(self, event, outcome):
        source = getattr(event, "source", None)
        if (getattr(source, "profile", None) == PROFILE
                and own_route_chat(getattr(source, "chat_id", ""))
                and Path(get_hermes_home()).resolve() != PROFILE_HOME):
            with WebhookAdapter._profile_scope(PROFILE):
                return await original(self, event, outcome)
        return await original(self, event, outcome)

    setattr(on_processing_complete, marker, True)
    WebhookAdapter.on_processing_complete = on_processing_complete


def register(ctx):
    install()
    install_session_close()
