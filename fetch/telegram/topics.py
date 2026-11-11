"""Where a topic's notification goes — 620's own destination, not 619's.

The ported orchestrator calls ``send_checked(text, topic="ops", ...)``, exactly
as 619's does, so that orchestrate.py stays byte-identical outside its two path
anchors (fetch/PORTING.md). In 619 the topic resolves to a forum supergroup and
a message thread; that routing is 619's and is deliberately NOT copied here.

620 keeps the destination its orchestrator always used: the chat named by the
``TELEGRAM_CHAT_ID`` environment variable, with no thread. Nothing in 620's
workflows sets it (or ``TELEGRAM_BOT_TOKEN``) today, so every send is a logged
no-op — the same behaviour as before the port, now through the checked sender.
"""
from __future__ import annotations

import os

# The only topic 620 sends to: pipeline health from the ICE fetch.
TOPICS = ("ops",)


def destination(topic: str) -> dict[str, str]:
    """sendMessage destination fields for `topic`. Raises ValueError when the
    topic is unknown or 620 has no chat configured for it."""
    if topic not in TOPICS:
        raise ValueError(f"unknown topic {topic!r} (620 knows {', '.join(TOPICS)})")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat:
        raise ValueError(f"no chat configured for topic {topic!r} (TELEGRAM_CHAT_ID unset)")
    return {"chat_id": chat}
