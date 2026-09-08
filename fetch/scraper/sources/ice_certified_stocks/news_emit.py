"""No-op stand-in for 619's news-feed commentary emitter.

The real module renders a commentary line from the day's certified-stock move
and stores it on a NewsItem, which the news.json exporter promotes. That is
engine output: it is what the system *says* about the data, not the data. It
stays in 619 and is deliberately not ported (see PORTING.md).

orchestrate.py imports it lazily inside run(), after the JSON has been written:

    from .news_emit import emit as _emit_news
    try:
        _emit_news(...)
    except Exception as e:
        ...

The call is guarded but the *import* is not, so with the module absent the whole
run died with ModuleNotFoundError after a successful 16-minute fetch — the data
was already staged and was thrown away anyway. This stub keeps orchestrate.py
byte-identical to 619's rather than editing that import out, which is the whole
basis of the shadow comparison.

Emitting nothing is correct here, not merely convenient: 620 has no news feed,
no database and no commentary to write.
"""

from __future__ import annotations


def emit(*_args, **_kwargs) -> None:
    """Accept the call and do nothing. 620 publishes data, not commentary."""
    return None
