import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webhook_ids  # noqa: E402

# Recorded from Hermes 0.21.5 for profile "acme", route "herdr-acme".
V2 = "webhook:v2:WyJhY21lIiwiaGVyZHItYWNtZSIsImZmNDY1MjhkMTU5NDQwZjM5N2EyZmNjNGI3MjA5YTJlIl0"
DELIVERY = "ff46528d159440f397a2fcc4b7209a2e"


def test_candidates_cover_the_legacy_and_v2_formats():
    assert webhook_ids.session_chat_ids("herdr-acme", DELIVERY, "acme") == [
        f"webhook:herdr-acme:{DELIVERY}", V2]


def test_unbound_route_encodes_the_default_profile():
    legacy, v2 = webhook_ids.session_chat_ids("herdr-acme", DELIVERY, None)
    assert webhook_ids.parse(v2) == ("default", "herdr-acme", DELIVERY)


def test_parse_both_formats():
    assert webhook_ids.parse(V2) == ("acme", "herdr-acme", DELIVERY)
    assert webhook_ids.parse(f"webhook:herdr-acme:{DELIVERY}") == (None, "herdr-acme", DELIVERY)


def test_parse_rejects_other_chats():
    for chat in ("", "123456789", "webhook:v2:not-base64!", "webhook:v2:" + "e30", "telegram:1"):
        assert webhook_ids.parse(chat) is None, chat
