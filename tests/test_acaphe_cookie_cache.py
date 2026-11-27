"""The cached acaphe session must leave Redis before acaphe kills it.

Register #262 (619), the cookie-cache half. acaphe expires its SSID session
server-side minutes after login. Across 620's 400 poll runs of 2026-11-18..27,
a cached jar was accepted at ages up to 2.9 min (6 runs) and refused with
"No Permission" from 4.2 min on (391 runs). Ticks are 15-20 min apart, so the
old 12 h jar TTL made almost every tick read a dead jar, send a doomed request,
print an ERROR and log in anyway.

The fake below is acaphe plus Upstash: sessions live SESSION_LIFE_S (the longest
age a jar was ever accepted, so the most pessimistic lifetime the logs allow),
and the store honours `SET ... EX` the way Upstash does.

Positive control: with the code before this change (COOKIE_TTL_S = 12 * 3600),
every case of `test_tick_after_the_session_died_logs_in_without_a_doomed_request`
fails — the second tick reuses the dead jar, the quote request is refused and
"cached session rejected" is printed — and
`test_jar_ttl_sits_inside_the_measured_session_lifetime` fails on its bound.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fetch"))

from scraper import acaphe_poller as ap

# Longest observed accepted age: login flushed 2026-11-27T09:16:20Z (run
# 45972211376), reuse flushed 09:19:14Z (run 45972639132) = 174 s. Shortest
# observed refused age: 2026-11-26T18:16:05Z -> 18:20:14Z = 249 s (runs
# 45874465007 -> 45875016512). The real lifetime lies in [174, 249) s.
SESSION_LIFE_S = 174
SHORTEST_REFUSED_AGE_S = 249

UPSTASH = "https://upstash.invalid"

VN_HIGH = ("09:46 29/10 (x) BMT bid 86000-86500 / offer 87000-87500 "
           "HCM bid 88000-88500 / offer 89000-89500")
ROWS = [
    {"stt": "0", "Month": "RF26", "Last": "4500", "Change": "10"},
    {"stt": "1", "Month": "AH26", "Last": "380.5", "Change": "-1"},
    {"stt": "14", "High": VN_HIGH, "Low": "26,070 (11:26 Oct 29)"},
]


class _Resp:
    """Enough of requests.Response for _get_redis, _push_redis, fetch_and_save."""

    status_code = 200
    ok = True

    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)  # raises on "No Permission", as acaphe does


class World:
    """acaphe (login + iquote.php) and Upstash (GET / SET EX) on one clock."""

    def __init__(self):
        self.now = 0.0
        self.store: dict[str, tuple[str, float | None]] = {}
        self.sessions: dict[str, float] = {}
        self.logins = 0
        self.quotes: list[bool] = []   # one entry per iquote request: accepted?

    def revoke_all(self):
        self.sessions.clear()

    def post(self, url, headers=None, timeout=None, **kwargs):
        cmd = kwargs["json"]       # _push_redis sends the Redis command as json=
        assert url == UPSTASH and cmd[0] == "SET", cmd
        expires = self.now + int(cmd[4]) if len(cmd) > 4 and cmd[3] == "EX" else None
        self.store[cmd[1]] = (cmd[2], expires)
        return _Resp('{"result": "OK"}')

    def get(self, url, cookies=None, headers=None, timeout=None):
        if url.startswith(UPSTASH + "/get/"):
            value, expires = self.store.get(url.rsplit("/", 1)[1], (None, None))
            if expires is not None and self.now >= expires:
                value = None
            return _Resp(json.dumps({"result": value}))
        assert url.startswith(ap.API_URL), url
        issued = self.sessions.get((cookies or {}).get("SSID"))
        accepted = issued is not None and self.now - issued < SESSION_LIFE_S
        self.quotes.append(accepted)
        return _Resp(json.dumps(ROWS) if accepted else "No Permission\n\n\n\n")

    async def login(self):
        self.logins += 1
        ssid = f"s{self.logins}"
        self.sessions[ssid] = self.now
        return {"SSID": ssid}

    def jar(self):
        value, expires = self.store.get(ap.COOKIE_KEY, (None, None))
        return (json.loads(value) if value else None), expires


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World()
    monkeypatch.setattr(ap, "UPSTASH_URL", UPSTASH)
    monkeypatch.setattr(ap, "UPSTASH_TOKEN", "test-token")
    monkeypatch.setattr(ap.requests, "get", w.get)
    monkeypatch.setattr(ap.requests, "post", w.post)
    monkeypatch.setattr(ap, "playwright_login", w.login)
    monkeypatch.setattr(ap, "OUTPUT", tmp_path / "acaphe_live.json")
    monkeypatch.setattr(ap, "VIETNAM_LAST", tmp_path / "vietnam_last.json")
    monkeypatch.setattr(ap, "DATABASE_URL", "")
    monkeypatch.setattr(ap, "VN_ONLY", False)
    monkeypatch.setattr(sys, "argv", ["acaphe_poller", "--once"])
    return w


def _tick(world: World, capsys, at: float) -> tuple[int, str]:
    world.now = at
    with pytest.raises(SystemExit) as exc:
        asyncio.run(ap.main())
    return exc.value.code, capsys.readouterr().out


def test_jar_ttl_sits_inside_the_measured_session_lifetime():
    assert 0 < ap.COOKIE_TTL_S < SESSION_LIFE_S < SHORTEST_REFUSED_AGE_S


@pytest.mark.parametrize("gap_s", [180, 300, 900, 1200])
def test_tick_after_the_session_died_logs_in_without_a_doomed_request(world, capsys, gap_s):
    # 900 s and 1200 s are the cron cadences (trading hours, VN window).
    code, _ = _tick(world, capsys, 0)
    assert code == 0 and world.logins == 1 and world.quotes == [True]

    code, out = _tick(world, capsys, gap_s)
    assert code == 0
    assert world.quotes == [True, True], "a request was sent on a dead session"
    assert world.logins == 2
    assert "No Permission" not in out
    assert "cached session rejected" not in out
    assert "reusing cached session" not in out


@pytest.mark.parametrize("gap_s", [60, 120])
def test_tick_soon_after_a_login_reuses_the_session(world, capsys, gap_s):
    # A cron-job.org dispatch and a GitHub schedule a minute or two apart.
    _tick(world, capsys, 0)
    code, out = _tick(world, capsys, gap_s)
    assert code == 0
    assert world.logins == 1, "a live cached session was not reused"
    assert world.quotes == [True, True]
    assert "reusing cached session" in out


def test_login_stores_the_jar_with_the_session_ttl(world, capsys):
    _tick(world, capsys, 1000)
    jar, expires = world.jar()
    assert jar == {"SSID": "s1"}
    assert expires == 1000 + ap.COOKIE_TTL_S


def test_refused_cached_session_is_replaced_exactly_once(world, capsys):
    _tick(world, capsys, 0)
    world.revoke_all()             # acaphe drops the session early
    code, out = _tick(world, capsys, 60)
    assert code == 0
    assert world.logins == 2
    assert world.quotes == [True, False, True]
    assert out.count("cached session rejected") == 1
    assert world.jar() == ({"SSID": "s2"}, 60 + ap.COOKIE_TTL_S)

    # The replacement is what the next tick inside the TTL reuses.
    code, out = _tick(world, capsys, 120)
    assert code == 0 and world.logins == 2 and world.quotes[-1] is True
    assert "reusing cached session" in out


def test_refused_fresh_login_is_not_retried(world, capsys, monkeypatch):
    # acaphe refusing a session it has just issued is not a stale jar; one
    # login per tick, then the tick fails visibly.
    async def _dead_login():
        world.logins += 1
        return {"SSID": "never-valid"}

    monkeypatch.setattr(ap, "playwright_login", _dead_login)
    code, out = _tick(world, capsys, 0)
    assert code == 1
    assert world.logins == 1
    assert world.quotes == [False]
    assert "ERROR" in out and "cached session rejected" not in out
