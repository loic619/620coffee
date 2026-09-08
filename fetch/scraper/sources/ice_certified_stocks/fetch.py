"""URL templates + date-aware fetch helper for ICE certified-stock sources.

All 10 publicdocs URLs follow stable date-templated patterns (confirmed reachable
from CI runners with no Akamai block — both bare and browser UA returned 200).
Each fetch tries today first, then walks back N business days if the file is not
yet published (ICE typically publishes T-1 / T+0 mid-day).
"""
from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import requests

# Browser UA isn't required (probe showed bare data-center UA also returns 200),
# but it's a tiny insurance against future bot-fingerprinting tightening.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# URL templates (date placeholders are bare format strings, filled by callers).
# YYYYMMDD = 8-digit; YYMMDD = 6-digit (the legacy LIFFE format).
ARABICA_DAILY_XLS         = "https://www.ice.com/publicdocs/futures_us_reports/coffee/coffee_cert_stock_{yyyymmdd}.xls"
# Monthly Arabica ageing report — published on the last business day of
# each calendar month. Caller fills in YYYYMMDD = previous month-end.
ARABICA_AGEING_XLS        = "https://www.ice.com/publicdocs/futures_us_reports/coffee/coffee_aging_{yyyymmdd}.xls"
ROBUSTA_STOCK_REPORT_CSV  = "https://www.ice.com/marketdata/publicdocs/liffe/coffee/stock_reports/Stock_Report_RC_{yyyymmdd}_{hhmmss}.csv"
ROBUSTA_AGE_ALLOWANCE_XLSX = "https://www.ice.com/marketdata/publicdocs/liffe/coffee/aged_allowance_stock_report/Robusta_Coffee_Age_Allowance_{yyyymmdd}.xlsx"
ROBUSTA_GRADING_OVERVIEW_PDF = "https://www.ice.com/marketdata/publicdocs/liffe/coffee/grading_overview/GradingOverviewCoffee_{yymmdd}.pdf"
ROBUSTA_GRADINGS_TXT      = "https://www.ice.com/marketdata/publicdocs/liffe/coffee/gradings/gradrc_{yymmdd}-{n}.txt"
ROBUSTA_ISS_RECV_DAILY    = "https://www.ice.com/marketdata/publicdocs/liffe/coffee/daily_issuers_receivers/irrrc_{yymmdd}.txt"
ROBUSTA_ISS_RECV_MONTHLY  = "https://www.ice.com/marketdata/publicdocs/liffe/coffee/monthly_issuers_receivers/irrrc_m{yymmdd}.txt"
ROBUSTA_GRADING_APPEALS   = "https://www.ice.com/marketdata/publicdocs/liffe/coffee/grading_appeals/apprc_{yymmdd}-{n}.txt"
ROBUSTA_TENDERS           = "https://www.ice.com/marketdata/publicdocs/liffe/coffee/tenders/tendrc_{yymmdd}.txt"
ROBUSTA_INFESTED_WARRANT  = "https://www.ice.com/marketdata/publicdocs/liffe/coffee/infested_warrant_report/INFESTEDCOFFEEWARRANT_{yymmdd}.pdf"


def yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def yymmdd(d: date) -> str:
    return d.strftime("%y%m%d")


def month_end_publish_candidates(year: int, month: int) -> list[date]:
    """Candidate publish dates for a month's Arabica ageing report, best-guess
    order. ICE dates the file around month-end but the exact day drifts: it can
    be the last calendar day (Mar→0331, Apr→0430, May→0531) or a few days
    earlier when month-end lands on a weekend/holiday (Jan 2026→0130,
    Feb 2026→0227), and occasionally a day or two into the next month. We try
    the last calendar day first, then walk back up to 3 days, then +1/+2.
    Weekends are included because at least one observed file (May) is dated on
    the weekend month-end itself."""
    last = date(year, month, calendar.monthrange(year, month)[1])
    out: list[date] = []
    seen: set[date] = set()
    for delta in (0, -1, -2, -3, 1, 2):
        d = last + timedelta(days=delta)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def business_days_back(start: date, n: int) -> Iterable[date]:
    """Yield `start`, then walk back n weekdays (skips Sat/Sun). ICE itself
    doesn't publish on US/EU holidays — caller treats a 404 as "try yesterday"."""
    yielded = 0
    cur = start
    while yielded <= n:
        if cur.weekday() < 5:
            yield cur
            yielded += 1
        cur -= timedelta(days=1)


@dataclass
class FetchResult:
    url: str
    status: int | None
    content: bytes | None
    text: str | None
    used_date: date | None      # the date whose URL actually succeeded
    error: str | None           # human-readable failure reason if no source-date worked


def fetch_text_with_dated_url(template: str, *, since: date, max_back: int = 5,
                              extra_fmt: dict | None = None) -> FetchResult:
    """Try `since`, then walk back up to `max_back` business days, formatting
    `template` with `yyyymmdd`/`yymmdd` + any `extra_fmt` keys.

    Returns the first 200 response (with both .content and .text), or a
    FetchResult with status/error set if nothing in the window works.
    """
    last_err = ""
    for d in business_days_back(since, max_back):
        url = template.format(yyyymmdd=yyyymmdd(d), yymmdd=yymmdd(d), **(extra_fmt or {}))
        try:
            r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
            if r.status_code == 200 and r.content:
                return FetchResult(url=url, status=200, content=r.content,
                                   text=r.text, used_date=d, error=None)
            last_err = f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
    return FetchResult(url=template, status=None, content=None, text=None,
                       used_date=None,
                       error=f"no source date in last {max_back+1} business days returned 200 ({last_err})")
