"""Hermes webhook session chat ids, in both formats Hermes has used (stdlib only).

- legacy:            ``webhook:<route>:<delivery_id>``
- v2 (Hermes 0.21.5+): ``webhook:v2:<urlsafe base64, no padding, of JSON [profile, route, delivery_id]>``,
  with profile ``"default"`` for a route not bound to a profile.

The bridge looks controller runs up by these ids; the plugins recognize their own route's turns.
"""
from __future__ import annotations

import base64
import binascii
import json

V2_PREFIX = "webhook:v2:"


def session_chat_ids(route: str, delivery_id: str, profile: str | None = None) -> list[str]:
    """Every chat id the run of this delivery may have: [legacy, v2]."""
    payload = json.dumps((profile or "default", route, delivery_id), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    v2 = V2_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return [f"webhook:{route}:{delivery_id}", v2]


def parse(chat_id: str) -> tuple[str | None, str, str] | None:
    """(profile, route, delivery_id) of a webhook session chat id; profile is None for legacy ids."""
    chat_id = str(chat_id or "")
    if chat_id.startswith(V2_PREFIX):
        token = chat_id[len(V2_PREFIX):]
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            parts = json.loads(raw.decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        if isinstance(parts, list) and len(parts) == 3 and all(isinstance(p, str) for p in parts):
            return parts[0], parts[1], parts[2]
        return None
    if chat_id.startswith("webhook:"):
        route, sep, delivery = chat_id[len("webhook:"):].partition(":")
        if route and sep and delivery:
            return None, route, delivery
    return None
