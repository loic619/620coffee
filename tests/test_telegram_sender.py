"""620's checked Telegram sender (port of 619's send_checked, 2026-11-09).

The old helper in orchestrate.py fired requests.post and discarded the answer,
so a 400 from Telegram looked exactly like a delivery. Positive control:
`test_rejected_message_is_not_a_delivery` would have passed silently against
that helper (it returned None whatever Telegram said) — here it must be False.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fetch"))

from telegram import sender, topics


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def posted(monkeypatch):
    calls = []

    def install(status, payload):
        import requests

        def fake_post(url, data, timeout):
            calls.append({"url": url, "data": dict(data)})
            return _Resp(status, payload)
        monkeypatch.setattr(requests, "post", fake_post)
        return calls
    return install


def test_confirmed_message_is_true(monkeypatch, posted):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    calls = posted(200, {"ok": True})
    assert sender.send_checked("hi", topic="ops", parse_mode=None, tag="t") is True
    assert calls[0]["data"] == {"text": "hi", "chat_id": "42"}   # no thread: 619's routing not copied


def test_rejected_message_is_not_a_delivery(monkeypatch, posted, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    posted(400, {"ok": False, "description": "Bad Request: chat not found"})
    assert sender.send_checked("hi", topic="ops", parse_mode=None, tag="t") is False
    err = capsys.readouterr().err
    assert "HTTP 400" in err and "chat not found" in err


def test_token_is_redacted_from_network_errors(monkeypatch, capsys):
    import requests
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def boom(url, data, timeout):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setattr(requests, "post", boom)
    assert sender.send_checked("hi", topic="ops", tag="t") is False
    err = capsys.readouterr().err
    assert "123:secret" not in err and "<token>" in err


def test_no_chat_configured_sends_nothing(monkeypatch, posted):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    calls = posted(200, {"ok": True})
    assert sender.send_checked("hi", topic="ops", tag="t") is False
    assert calls == []


def test_unknown_topic_is_refused():
    with pytest.raises(ValueError):
        topics.destination("market")


def test_orchestrator_routes_through_checked_sender(monkeypatch, posted, tmp_path):
    monkeypatch.setenv("ICE_STAGE_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    from scraper.sources.ice_certified_stocks import orchestrate
    calls = posted(200, {"ok": True})
    orchestrate._telegram("late release", tag="robusta")
    assert len(calls) == 1 and calls[0]["data"]["chat_id"] == "42"
