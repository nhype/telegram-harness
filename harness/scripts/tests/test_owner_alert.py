import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import herdr_event_bridge as bridge  # noqa: E402

TOKEN = "123456789:" + "B" * 35


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_alert_goes_to_the_owner_chat_with_the_profile_bot(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text(f"OTHER=1\nTELEGRAM_BOT_TOKEN={TOKEN}\n")
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, data, timeout: calls.append((url, data)) or FakeResponse())
    assert bridge.send_owner_alert(str(env), "111111111", "task stuck")
    url, data = calls[0]
    assert url.endswith(f"bot{TOKEN}/sendMessage")
    assert urllib.parse.parse_qs(data.decode()) == {"chat_id": ["111111111"], "text": ["task stuck"]}
    assert TOKEN not in capsys.readouterr().out


def test_alert_failure_never_logs_the_token_or_raises(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n")

    def boom(url, data, timeout):
        raise OSError(f"cannot reach {url}")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert not bridge.send_owner_alert(str(env), "111111111", "x")
    assert not bridge.send_owner_alert(str(tmp_path / "missing.env"), "111111111", "x")
    assert TOKEN not in capsys.readouterr().out
