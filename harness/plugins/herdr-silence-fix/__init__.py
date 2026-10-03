"""Profile-local compatibility patch for Herdr webhook silence.

Only delivery/presentation classification changes; event.internal is NOT changed.
Remove once Hermes natively classifies autonomous webhook turns for silence.
"""
from functools import wraps
import json
import logging
from pathlib import Path
import sys

from hermes_constants import get_hermes_home


def _settings():
    """(profile home, route) from <HERMES_HOME>/herdr-pipeline.json; (None, '') if unset."""
    home = Path(get_hermes_home()).resolve()
    try:
        data = json.loads((home / "herdr-pipeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ""
    route = data.get("route") if isinstance(data, dict) else None
    return (home, route) if isinstance(route, str) and route else (None, "")


PROFILE_HOME, _ROUTE = _settings()
ROUTE_USER = f"webhook:{_ROUTE}" if _ROUTE else ""


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
            and str(getattr(source, "chat_id", "")).startswith(ROUTE_USER + ":")
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


def register(ctx):
    install()
