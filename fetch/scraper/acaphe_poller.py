"""
acaphe_poller.py — poll iquote.php, write cleaned JSON to a scratch directory
and push it to Redis.

PORTED TO 620 — the docstring above is the first of three divergences from
619's copy; the other two are the OUTPUT / VIETNAM_LAST anchors below. 619
writes into its frontend's public data directory. Everything else in this file
is byte-identical, deliberately: Redis is the live path for both repositories,
so a difference in what reaches it would have to mean the poll differed, not
the code.

Usage (from repo root):
    python fetch/scraper/acaphe_poller.py              # persistent loop
    python -m scraper.acaphe_poller --once             # one tick (CI cron)

Two things are worth knowing before changing this file.

Playwright is the exception path, not the normal one. Only the LOGIN needs a
browser; the quotes themselves come over plain HTTP with the resulting
cookies. Those cookies are cached in Redis (see get_cookies), so a --once tick
in a fresh CI container reuses the session instead of launching Chromium every
time. A browser starts only on a cache miss, or when cached cookies are
rejected.

A successful fetch is not the same as usable data. acaphe answers HTTP 200
with an empty or unpriced payload often enough that publishing whatever parsed
would blank the live panel — and refresh the key's timestamp, hiding the
outage from the 1.8 freshness checker. classify() gates the publish: degraded
payloads are logged and the last good snapshot is left in place.
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# PORTED TO 620 — path anchors. 619 writes these into frontend/public/data,
# where its panel reads them as a last-resort fallback behind Redis. 620 has no
# frontend, and these quotes come from a LOGIN-GATED source, so they are not
# committed to this public repository: they are per-run scratch that dies with
# the runner. The live path is the Upstash push, which is unchanged. See
# docs/ACAPHE.md for what that costs and what it does not.
ACAPHE_STAGE  = Path(os.environ.get(
    "ACAPHE_STAGE_DIR", Path(__file__).parents[2] / "_stage" / "acaphe")).resolve()
ACAPHE_STAGE.mkdir(parents=True, exist_ok=True)
OUTPUT        = ACAPHE_STAGE / "acaphe_live.json"
VIETNAM_LAST  = ACAPHE_STAGE / "vietnam_last.json"
UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_KEY     = "live_quotes"
# Session jar, cached so Playwright is not launched on every tick. Stored in
# Upstash (token-gated, not public like this repo) with a TTL so a credential
# cannot linger indefinitely; acaphe's own session may expire sooner, which
# the fetch-then-relogin path below handles.
COOKIE_KEY    = "acaphe_cookies"
COOKIE_TTL_S  = 12 * 3600
DATABASE_URL  = os.environ.get("DATABASE_URL", "")

# VN-only mode, set by the 01:00-04:00 UTC cron block in poll-acaphe-quotes.yml.
# Those ticks exist purely to catch the Vietnamese morning, when acaphe publishes
# the Dak Lak / HCM bid-offer and the R2 FOB differential. London and New York
# are both closed at that hour, so the futures half of the payload is off-session
# — pushing it to the `live_quotes` key would overwrite a good snapshot with an
# out-of-session one and refresh its timestamp, which would also blind the 1.8
# freshness checker to a genuinely dead feed. In this mode we still write the VN
# snapshot (file + `vietnam_last` Redis key + DB) and skip only the live_quotes
# push.
VN_ONLY       = os.environ.get("ACAPHE_VN_ONLY", "").lower() in ("1", "true", "yes")


def _push_redis(data: dict, key: str = REDIS_KEY, ttl_s: int | None = None) -> None:
    """Push data to Upstash Redis via REST API. Silent no-op if not configured.

    `ttl_s` sets an expiry (Redis SET ... EX). Used for the cookie jar so a
    session credential cannot outlive its usefulness in the store.
    """
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        cmd = ["SET", key, payload]
        if ttl_s:
            cmd += ["EX", str(int(ttl_s))]
        resp = requests.post(
            UPSTASH_URL,
            headers={
                "Authorization": f"Bearer {UPSTASH_TOKEN}",
                "Content-Type":  "application/json",
            },
            json=cmd,
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"[acaphe][redis] push failed ({key}): {exc}")


def _get_redis(key: str):
    """Read a JSON value back from Upstash. None when unset/unreachable."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        r = requests.get(
            f"{UPSTASH_URL}/get/{key}",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            timeout=5,
        )
        if not r.ok:
            return None
        result = r.json().get("result")
        if not result:
            return None
        return json.loads(result) if isinstance(result, str) else result
    except Exception as exc:
        print(f"[acaphe][redis] get failed ({key}): {exc}")
        return None
API_URL       = "https://acaphe.com/iquote.php?v="
LOGIN_URL     = "https://acaphe.com/"
# Credentials come from the environment (ACAPHE_USER / ACAPHE_PASS), set as
# GitHub secrets on the poll-acaphe-quotes workflow. Never hardcode them — this
# repo is public. The old default was rotated out; there is no in-code fallback.
USERNAME      = os.environ.get("ACAPHE_USER", "")
PASSWORD      = os.environ.get("ACAPHE_PASS", "")
POLL_INTERVAL = 30   # seconds between polls
RELOGIN_AFTER = 3    # consecutive failures before re-login

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer":          "https://acaphe.com/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept":           "application/json, text/plain, */*",
}


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _parse_oi(s: str) -> tuple[int | None, int | None]:
    """'10898 (-1177)' → (10898, -1177)"""
    m = re.match(r"([\d,]+)\s*\(([+-]?[\d,]+)\)", str(s or ""))
    if not m:
        return None, None
    return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))


def _parse_52wk(s: str) -> tuple[float | None, float | None]:
    """'5029 / 3084' → (5029.0, 3084.0)"""
    parts = str(s or "").split(" / ")
    if len(parts) != 2:
        return None, None
    try:
        return float(parts[0].strip()), float(parts[1].strip())
    except ValueError:
        return None, None


def _parse_chg_pct(s: str) -> float | None:
    """'<span ...>(2.77%)</span>' → 2.77"""
    m = re.search(r"\(([\d.]+)%\)", s or "")
    return float(m.group(1)) if m else None


def _parse_vietnam(row14: dict) -> dict:
    raw = row14.get("High", "")
    # Diagnostic: when the BMT/HCM patterns stop matching (acaphe rewords the
    # free-text block now and then), log what the row actually carries so the
    # scheduled runs document the new format instead of failing silently.
    if raw and "BMT bid" not in raw:
        print(f"[acaphe] VN row14.High unrecognised: {raw[:300]!r}")
    elif not raw:
        print(f"[acaphe] VN row14 empty High; row keys/preview: "
              f"{ {k: str(v)[:60] for k, v in row14.items()} }")

    # Local time: "09:46 21/04 (...)"
    tm = re.match(r"(\d{2}:\d{2}\s+\d{2}/\d{2})", raw)
    local_time = tm.group(1) if tm else None

    # BMT bid/offer
    bmt = re.search(r"BMT bid ([\d]+-[\d]+)\s*/\s*offer\s+([\d]+-[\d]+)", raw)
    # HCM bid/offer
    hcm = re.search(r"HCM bid ([\d]+-[\d]+)\s*/\s*offer\s+([\d]+-[\d]+)", raw)
    # R2 FOB differential
    r2  = re.search(r"R2 FOB.*?bid\s*([+-]?\d+)\s*/\s*offer\s*([+-]?\d+)", raw)
    # Pepper. Non-greedy gap so [^(]* doesn't swallow the leading digits of the
    # first number (greedy "Pepper[^(]*" turned "136-138000" into "6-138000").
    pep = re.search(r"Pepper[^(]*?([\d]+-[\d,]+)", raw)

    # USD/VND (VCB) rate lives in row14['Low']: "26,070 (11:26 Jul 15)" → 26070.
    # (The old source, whenldclose="105615(166760)", is NOT the FX rate — its
    # first number is a VND figure ~4x too high, which is what put 105,615 in
    # the Vietnam Local Prices panel.)
    low = str(row14.get("Low", "") or "")
    vcm = re.match(r"\s*([\d.,]+)", low)
    usd_vnd = int(re.sub(r"\D", "", vcm.group(1))) if vcm and re.sub(r"\D", "", vcm.group(1)) else None

    return {
        "local_time":  local_time,
        "bmt_bid":     bmt.group(1) if bmt else None,
        "bmt_offer":   bmt.group(2) if bmt else None,
        "hcm_bid":     hcm.group(1) if hcm else None,
        "hcm_offer":   hcm.group(2) if hcm else None,
        "r2_fob_bid":  r2.group(1)  if r2  else None,
        "r2_fob_offer":r2.group(2)  if r2  else None,
        "pepper_faq":  pep.group(1) if pep else None,
        "usd_vnd":     usd_vnd,
    }


def _safe_float(s) -> float | None:
    try:
        return float(str(s or "0").replace(",", ""))
    except (ValueError, TypeError):
        return None


def _safe_int(s) -> int | None:
    try:
        return int(str(s or "0").replace(",", ""))
    except (ValueError, TypeError):
        return None


def transform(raw: list) -> dict:
    """Convert the 15-row iquote.php response into a clean dict."""
    robusta: list[dict] = []
    arabica: list[dict] = []
    row14: dict | None  = None

    for row in raw:
        stt = int(row.get("stt", -1))
        if stt == 14:
            row14 = row
            continue

        month = row.get("Month", "")
        is_arabica = month.startswith("A")

        change = _safe_float(row.get("Change")) or 0.0
        chg_pct = _parse_chg_pct(row.get("Change_per", ""))
        if chg_pct is not None and change < 0:
            chg_pct = -chg_pct

        oi, oi_chg = _parse_oi(row.get("OpInt", ""))
        w52h, w52l = _parse_52wk(row.get("Time", ""))

        entry = {
            "month":       month,
            "change":      change,
            "change_pct":  chg_pct,
            "last":        _safe_float(row.get("Last")),
            "vol":         _safe_int(row.get("Vol")),
            "high":        _safe_float(row.get("High")),
            "low":         _safe_float(row.get("Low")),
            "open":        _safe_float(row.get("Open")),
            "prev":        _safe_float(row.get("Prev")),
            "oi":          oi,
            "oi_chg":      oi_chg,
            "week52_high": w52h,
            "week52_low":  w52l,
            # Only front months carry LTD/FND dates
            "opt_ltd":     row.get("Opt_LTD") or None,
            "fut_fnd":     row.get("Fut_FND") or None,
        }

        (arabica if is_arabica else robusta).append(entry)

    result: dict = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "now_time":   raw[0].get("now_time", "") if raw else "",
        "robusta":    robusta,
        "arabica":    arabica,
    }

    if row14:
        result["vietnam"] = _parse_vietnam(row14)
        result["spreads"] = {
            "robusta": row14.get("Time", ""),
            "arabica": row14.get("Date", ""),
        }
        result["arb_ratio"] = row14.get("TimeV", "")
        result["equities"]  = _strip_html(row14.get("timelife") or row14.get("nyld", ""))

    return result


# ── Source validation ─────────────────────────────────────────────────────────

def classify(data: dict) -> tuple[str, list[str]]:
    """Is this payload usable market data, or merely a successful HTTP call?

    HTTP 200 + parseable JSON is NOT the same as "acaphe published quotes".
    acaphe intermittently serves the bare stub `acaphe()` in place of the
    block, and can return the row scaffolding with every price field empty.
    Both used to sail through: fetch_and_save returned True for anything that
    parsed, so an empty payload was pushed over a good snapshot in Redis and
    the panel went blank with a fresh timestamp on it — which also blinded the
    1.8 freshness checker, since the key had just been written.

    Returns (status, reasons):
      healthy  — at least one priced contract on each board
      degraded — parsed, but the futures are missing or unpriced

    "dead" is not returned here: a timeout, HTTP error or unparseable body
    raises before this is reached and is handled by the caller.
    """
    reasons: list[str] = []
    for board in ("robusta", "arabica"):
        rows = data.get(board) or []
        if not rows:
            reasons.append(f"{board}: no rows")
            continue
        if not any(r.get("last") is not None for r in rows):
            reasons.append(f"{board}: {len(rows)} rows, none priced")
    return ("degraded" if reasons else "healthy"), reasons


# ── Network helpers ────────────────────────────────────────────────────────────

async def playwright_login() -> dict:
    """Login via Playwright, return session cookies."""
    from playwright.async_api import async_playwright

    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "ACAPHE_USER / ACAPHE_PASS not set — configure them as workflow "
            "secrets (see poll-acaphe-quotes.yml). Refusing to log in without "
            "credentials.")

    print("[acaphe] Logging in via Playwright …")
    last_err = None
    async with async_playwright() as pw:
        for attempt in (1, 2, 3):
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(user_agent=HEADERS["User-Agent"])
                page = await ctx.new_page()

                # domcontentloaded, NOT networkidle: the logged-in page streams
                # live quotes and never goes network-idle, so `networkidle` timed
                # out (Timeout Nms exceeded) even on a perfectly good login. The
                # DOM-ready signal settles fast and reliably.
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

                inputs = await page.query_selector_all("input")
                input_types = [await i.get_attribute("type") or "text" for i in inputs]
                print(f"[acaphe] Form inputs found: {input_types}")

                await page.fill('input[type="text"]',     USERNAME)
                await page.fill('input[type="password"]', PASSWORD)

                async with page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                    await page.click('input[type="submit"]')

                final_url = page.url
                title = await page.title()
                cookie_dict = {c["name"]: c["value"] for c in await ctx.cookies()}
                logged_in = await page.query_selector('input[type="password"]') is None
                print(f"[acaphe] Post-login — url={final_url} | title={title!r} | "
                      f"logged_in={logged_in} | cookies={list(cookie_dict.keys())}")
                if not logged_in:
                    print("[acaphe] WARNING: password field still present — creds may be wrong or form changed")
                return cookie_dict
            except Exception as e:  # noqa: BLE001 — retry transient acaphe/browser hiccups
                last_err = repr(e)
                print(f"[acaphe] login attempt {attempt}/3 failed: {last_err}")
            finally:
                await browser.close()
            if attempt < 3:
                await asyncio.sleep(3 * attempt)   # 3s, 6s backoff

    raise RuntimeError(f"acaphe login failed after 3 attempts: {last_err}")


async def get_cookies(force_login: bool = False) -> tuple[dict, bool]:
    """Session cookies for iquote.php, preferring the cached jar.

    Under GitHub Actions every tick is a fresh container, so "log in once then
    poll" — the shape the persistent worker uses — has nothing to hold the
    session in. Caching the jar in Redis (already provisioned for the quotes
    themselves) restores that property across runs: Playwright then runs only
    on a cache miss or after the cached cookies stop working, instead of on
    every single tick.

    The jar carries a TTL so a stale credential expires on its own, and the
    caller re-logs in when a fetch fails on cached cookies.

    Returns (cookies, from_cache) — the flag tells the caller whether a
    failure is worth retrying with a fresh login.
    """
    if not force_login:
        cached = _get_redis(COOKIE_KEY)
        if cached:
            print(f"[acaphe] reusing cached session ({len(cached)} cookies) — no browser needed")
            return cached, True
    cookies = await playwright_login()
    _push_redis(cookies, key=COOKIE_KEY, ttl_s=COOKIE_TTL_S)
    return cookies, False


def _save_vn_prices_to_db(viet: dict, fetched_at: str) -> None:
    """Store VN local prices to Postgres — one row per calendar day (UTC).
    Subsequent appearances of VN data within the same day are skipped so the
    DB accumulates one clean daily record for trend analysis."""
    import os
    import sys
    backend_dir = str(Path(__file__).parents[1])
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    try:
        os.environ.setdefault("DATABASE_URL", DATABASE_URL)
        from datetime import datetime

        from scraper.db import (
            create_vn_local_prices_table,
            get_latest_vn_local_price,
            upsert_vn_local_price,
        )
        create_vn_local_prices_table()
        recorded_at = datetime.fromisoformat(fetched_at.replace("Z", "+00:00")).replace(tzinfo=None)
        today_str   = recorded_at.date().isoformat()

        latest = get_latest_vn_local_price()
        if latest:
            last_date = (latest.get("saved_at") or "")[:10]
            if last_date == today_str:
                print(f"[acaphe] VN prices for {today_str} already saved — skipping")
                return

        upsert_vn_local_price(viet, recorded_at)
        print(f"[acaphe] Vietnam prices saved to DB ({today_str})")
    except Exception as exc:
        print(f"[acaphe][db] write failed: {exc}")


def fetch_and_save(cookies: dict) -> bool:
    """Fetch iquote.php, transform, write to OUTPUT. Returns True on success."""
    backend_dir = str(Path(__file__).parents[1])
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from scraper.validate_export import safe_write_json
    url = f"{API_URL}{int(time.time() * 1000)}"
    try:
        resp = requests.get(url, cookies=cookies, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        raw  = resp.json()
        data = transform(raw)
        safe_write_json(OUTPUT, data, ensure_ascii=False)

        # Vietnam persistence: save when live, inject last-known when absent
        viet = data.get("vietnam") or {}
        if viet.get("bmt_bid") or viet.get("hcm_bid"):
            # VN data present — save snapshot to file, Redis, and DB (once per day)
            snapshot = {**viet, "saved_at": data["fetched_at"]}
            safe_write_json(VIETNAM_LAST, snapshot, ensure_ascii=False)
            _push_redis(snapshot, key="vietnam_last")
            print("[acaphe] Vietnam snapshot saved (local + Redis)")
            if DATABASE_URL:
                _save_vn_prices_to_db(viet, data["fetched_at"])
        else:
            # VN data absent — inject the last-known snapshot into live_quotes
            # so the UI never shows an empty VN panel after morning data disappears
            last_vn = _get_redis("vietnam_last")
            if last_vn:
                data["vietnam"] = last_vn
                print("[acaphe] VN data absent — injected last-known snapshot")

        # Validate before publishing: a degraded payload must not overwrite the
        # last good one (see classify()). The status travels with the data so
        # the frontend and the freshness checker can tell "acaphe answered" from
        # "acaphe published quotes".
        status, reasons = classify(data)
        data["status"] = status

        if VN_ONLY:
            print("[acaphe] VN-only tick — skipping live_quotes push (futures off-session)")
        elif status == "healthy":
            _push_redis(data)
        else:
            # Retained, not republished. Exit stays 0: acaphe serving a stub is
            # not this job failing, and turning every upstream hiccup red would
            # bury the real ones — staleness is already alerted on by 1.8, which
            # keeps working precisely because we did NOT refresh the key here.
            print(f"::warning::acaphe payload degraded ({'; '.join(reasons)}) — "
                  "keeping the last good live_quotes rather than overwriting it")

        viet     = data.get("vietnam", {}) or {}
        bmt_bid  = viet.get("bmt_bid", "?")
        bmt_off  = viet.get("bmt_offer", "?")
        usd_vnd  = viet.get("usd_vnd", "?")
        r_last   = data["robusta"][0]["last"]  if data["robusta"]  else "?"
        a_last   = data["arabica"][0]["last"]  if data["arabica"]  else "?"
        now_t    = data.get("now_time", "")
        print(
            f"[acaphe] {datetime.now().strftime('%H:%M:%S')} | {now_t} | {status} | "
            f"RC={r_last} KC={a_last} | BMT {bmt_bid}/{bmt_off} | VCB={usd_vnd}"
        )
        return True
    except Exception as exc:
        try:
            print(f"[acaphe] ERROR: {exc} | status={resp.status_code} | body={resp.text[:300]!r}")
        except Exception:
            print(f"[acaphe] ERROR: {exc}")
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main():
    """
    Default: log in once via Playwright, then poll iquote.php every
    POLL_INTERVAL seconds forever (intended for a long-running worker
    on Render / your laptop / any always-on host).

    With --once: log in, fetch a single snapshot, push to Upstash, exit.
    Used by .github/workflows/poll-acaphe-quotes.yml so the dashboard
    stays fresh without paying for a 24/7 worker — GitHub Actions runs
    this on a 5-minute cron for free on public repos.
    """
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--once", action="store_true",
                        help="Single fetch + push then exit (cron-friendly).")
    args = parser.parse_args()

    cookies, from_cache = await get_cookies()

    if args.once:
        ok = fetch_and_save(cookies)
        if not ok and from_cache:
            # The cached jar is the only thing that could be stale here, and a
            # fresh login is exactly the recovery for it. Only worth a browser
            # when we had not already just used one.
            print("[acaphe] cached session rejected — logging in again …")
            cookies, _ = await get_cookies(force_login=True)
            ok = fetch_and_save(cookies)
        # Non-zero exit on failure so the workflow surfaces the problem.
        sys.exit(0 if ok else 1)

    print(f"[acaphe] Polling every {POLL_INTERVAL}s → {OUTPUT}")
    fails = 0
    while True:
        ok = fetch_and_save(cookies)
        if ok:
            fails = 0
        else:
            fails += 1
            if fails >= RELOGIN_AFTER:
                print(f"[acaphe] {fails} consecutive failures — re-logging in …")
                cookies, _ = await get_cookies(force_login=True)
                fails   = 0

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
