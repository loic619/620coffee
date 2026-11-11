"""The one checked path to Telegram's sendMessage — ported from 619.

Port of 619's ``backend/telegram/sender.py`` (``send_checked``, merged there on
2026-11-09). The orchestrator imports it as ``telegram.sender``; publish_ice.py
puts ``fetch/`` on sys.path, so this package is what resolves. Destinations are
620's own (``telegram.topics``): 619's group, threads and the interactive bot's
``send_message`` are not part of this port.

WHY "CHECKED". Telegram answers a rejected message (bad HTML, unknown chat,
over-long text, revoked token) with a 4xx and `{"ok": false, "description":
...}`. The previous helper fired `requests.post` and discarded the answer, so a
400 looked exactly like a delivery. `send_checked` returns True only when
Telegram confirmed the message: HTTP 200 AND a JSON body with `ok: true`.
Anything else returns False and logs the HTTP status and Telegram's
`description`.

THE TOKEN NEVER REACHES A LOG. It sits in the URL path, and `requests` puts the
URL into its exception messages. Every logged string goes through `_redact`.

NO THIRD-PARTY IMPORT AT MODULE LEVEL. `requests` is used when present;
otherwise the same request goes through urllib.

A REFUSED SEND IS LOUD. It is printed with the topic and chat and — inside
GitHub Actions — raised as a `::warning::` annotation on the run page.
"""
from __future__ import annotations

import json
import os
import sys

from . import topics as _topics

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _redact(s: object, token: str) -> str:
    s = str(s)
    return s.replace(token, "<token>") if token else s


def response_ok(status: int | None, body: object) -> tuple[bool, str]:
    """(confirmed, description) from an HTTP status and a raw or parsed body.

    Confirmed means status 200 and `{"ok": true}` — both, because a proxy or
    captive portal can answer 200 with an HTML page that is not a delivery.
    """
    payload = body
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
    if status == 200 and isinstance(payload, dict) and payload.get("ok") is True:
        return True, ""
    if isinstance(payload, dict) and payload.get("description"):
        desc = str(payload["description"])
    elif isinstance(body, str) and body:
        desc = body[:200]
    else:
        desc = "no ok:true in the response"
    return False, desc


def _post_requests(requests, url: str, data: dict, timeout: float) -> tuple[int | None, object]:
    resp = requests.post(url, data=data, timeout=timeout)
    status = getattr(resp, "status_code", None)
    try:
        return status, resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is a failure, not a crash
        return status, getattr(resp, "text", "") or ""


def _post_urllib(url: str, data: dict, timeout: float) -> tuple[int | None, object]:
    import urllib.error
    import urllib.parse
    import urllib.request
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:   # 4xx/5xx: Telegram's JSON is in the body
        return e.code, e.read()


def _annotate(msg: str) -> None:
    """Make a refused send visible on the Actions run page, not only in a step
    log nobody opens. Outside Actions this is a no-op."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=Telegram send failed::{msg}")


def send_checked(text: str, *, topic: str | None = None, chat_id: str | int | None = None,
                 token: str | None = None, parse_mode: str | None = "HTML",
                 tag: str = "telegram", timeout: float = 20, **extra: object) -> bool:
    """Send one message; True only if Telegram confirmed it (200 + ok:true).

    Destination: `topic` (see `telegram.topics`) or an explicit `chat_id` —
    exactly one of the two. `token` defaults to TELEGRAM_BOT_TOKEN, read at
    call time. Never raises: a notification must not fail the job that
    produced it — but the caller learns it was not delivered.
    """
    token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if topic is not None and chat_id is not None:
        print(f"[{tag}] telegram: pass a topic OR a chat_id, not both — not sent",
              file=sys.stderr)
        return False
    where = ""
    data: dict = {"text": text}
    if topic is not None:
        try:
            dest = _topics.destination(topic)
        except ValueError as e:
            print(f"[{tag}] telegram: {e} — not sent", file=sys.stderr)
            return False
        data.update(dest)
        where = f" (topic {topic}: chat {dest['chat_id']})"
    elif chat_id is not None and str(chat_id) != "":
        data["chat_id"] = chat_id
    else:
        print(f"[{tag}] telegram: no destination (topic or chat_id) — not sent",
              file=sys.stderr)
        return False
    if not token:
        print(f"[{tag}] telegram not configured (no TELEGRAM_BOT_TOKEN) — not sent")
        return False
    if parse_mode:
        data["parse_mode"] = parse_mode
    data.update({k: v for k, v in extra.items() if v is not None})
    url = API_URL.format(token=token)
    try:
        try:
            import requests
        except ImportError:
            status, body = _post_urllib(url, data, timeout)
        else:
            status, body = _post_requests(requests, url, data, timeout)
    except Exception as e:  # noqa: BLE001 — network faults are a failed send, not a crash
        msg = f"[{tag}] telegram send failed{where}: {type(e).__name__}: {_redact(e, token)}"
        print(msg, file=sys.stderr)
        _annotate(msg)
        return False
    ok, desc = response_ok(status, body)
    if not ok:
        msg = (f"[{tag}] telegram rejected the message{where}: HTTP {status} — "
               f"{_redact(desc, token)}")
        print(msg, file=sys.stderr)
        _annotate(msg)
    return ok
