"""
validate_export.py
Per-file validation for static JSON exports.

Each validate_* function receives the in-memory payload and returns
(passed: bool, reason: str).  Called by safe_write_json before any write.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

# ── helpers ───────────────────────────────────────────────────────────────────

def _days_since_iso(iso_str: str) -> float:
    """Days since an ISO-8601 datetime string (handles Z and +00:00)."""
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds() / 86400
    except Exception:
        return float("inf")


def _days_since_date(date_str: str) -> int:
    """Days since a YYYY-MM-DD date string."""
    try:
        return (date.today() - date.fromisoformat(date_str)).days
    except Exception:
        return 9999


def _first_number(value) -> float | None:
    """Parse the leading numeric value out of a ticker `value` field.

    Ticker values are display strings that mix locales — US style
    ('100,000 VND ($1,234)', 'Q1,875.87', '273.45') where ',' groups thousands
    and '.' is the decimal, and Brazilian/European style ('1.050,00 BRL',
    '100.000 VND') where it's the reverse. We extract the leading number and
    normalise both conventions so day-to-day comparison is apples-to-apples:
        '100,000 VND ($1,234)' → 100000.0
        '1.050,00 BRL (...)'   → 1050.0
        '1.0552 (…)'           → 1.0552
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    m = re.search(r"-?\d[\d.,]*", value)
    if not m:
        return None
    tok = m.group(0).rstrip(".,")
    has_dot, has_comma = "." in tok, "," in tok
    if has_dot and has_comma:
        # Rightmost separator is the decimal; the other is a thousands grouping.
        if tok.rfind(",") > tok.rfind("."):
            tok = tok.replace(".", "").replace(",", ".")   # BR: 1.050,00 → 1050.00
        else:
            tok = tok.replace(",", "")                      # US: 1,875.87 → 1875.87
    elif has_comma:
        # A single ',' with a non-3-digit tail is a decimal ('980,00'); a 3-digit
        # tail (or several groups) is a thousands grouping ('100,000').
        parts = tok.split(",")
        tok = tok.replace(",", ".") if len(parts) == 2 and len(parts[1]) != 3 else tok.replace(",", "")
    elif has_dot:
        # Same rule for '.': '1.0552' is a decimal, '100.000' is thousands.
        parts = tok.split(".")
        if not (len(parts) == 2 and len(parts[1]) != 3):
            tok = tok.replace(".", "")
    try:
        return float(tok)
    except ValueError:
        return None


# ── cross-run sanity (volatility) guards ──────────────────────────────────────

def price_swing_guard(threshold: float = 0.30):
    """Build an (old, new) -> (ok, reason) guard for latest_prices-shaped data.

    Rejects the new payload when any ticker's leading numeric value moved more
    than `threshold` (fractional) versus the same-label ticker in the previous
    good file. This catches unit/parse breaks that pass type, shape and
    freshness checks — e.g. the VN FAQ price read as 10,000 instead of 100,000
    VND, or an FX rate dropping a digit.
    """
    def _check(old: dict, new: dict) -> tuple[bool, str]:
        if not isinstance(old, dict) or not isinstance(new, dict):
            return True, "ok"  # nothing comparable → don't block
        old_by_label = {
            t.get("label"): t.get("value")
            for t in (old.get("tickers") or [])
            if isinstance(t, dict)
        }
        for t in (new.get("tickers") or []):
            if not isinstance(t, dict):
                continue
            new_v = _first_number(t.get("value"))
            old_v = _first_number(old_by_label.get(t.get("label")))
            if old_v is None or new_v is None or old_v == 0:
                continue
            change = abs(new_v - old_v) / abs(old_v)
            if change > threshold:
                return False, (
                    f"{t.get('label')} swung {change * 100:.0f}% "
                    f"({old_v:g} → {new_v:g}), suspected parsing error"
                )
        return True, "ok"

    return _check


def futures_chain_swing_guard(threshold: float = 0.30):
    """Build an (old, new) -> (ok, reason) guard for futures_chain.json.

    futures_chain.json is `{arabica|robusta: {contracts: [{symbol, last, …}]}}`.
    KC prints in cents (~334) and RC in dollars (~3872), so a units/parse break
    (KC read as dollars, a dropped digit) sails past the shape/freshness
    validator. This compares each contract's `last` to the same-`symbol` contract
    in the previous good file and rejects a > `threshold` move. Contracts absent
    from either side (roll-off / new listing) are skipped, not blocked.
    """
    def _check(old: dict, new: dict) -> tuple[bool, str]:
        if not isinstance(old, dict) or not isinstance(new, dict):
            return True, "ok"
        for market in ("arabica", "robusta"):
            old_by_sym = {
                c.get("symbol"): c.get("last")
                for c in ((old.get(market) or {}).get("contracts") or [])
                if isinstance(c, dict)
            }
            for c in ((new.get(market) or {}).get("contracts") or []):
                if not isinstance(c, dict):
                    continue
                new_v = _first_number(c.get("last"))
                old_v = _first_number(old_by_sym.get(c.get("symbol")))
                if old_v is None or new_v is None or old_v == 0:
                    continue
                change = abs(new_v - old_v) / abs(old_v)
                if change > threshold:
                    return False, (
                        f"{market} {c.get('symbol')} last swung {change * 100:.0f}% "
                        f"({old_v:g} → {new_v:g}), suspected units/parse error"
                    )
        return True, "ok"

    return _check


def fx_history_swing_guard(threshold: float = 0.25):
    """Build an (old, new) -> (ok, reason) guard for fx_history.json.

    fx_history.json is `{pairs: {TICKER: {history: [{date, close}, …]}}}`. The
    per-row merge already drops single-day outliers, but a systematic re-scaling
    (a whole pair rebased, a currency inverted) can shift every point uniformly
    and slip through. This compares each pair's *latest* close to the previous
    good file's latest close for that pair and rejects a > `threshold` move — no
    FX cross legitimately moves 25% day-over-day, so that's a parse break.
    """
    def _latest_close(pair: dict) -> float | None:
        hist = pair.get("history") if isinstance(pair, dict) else None
        if not isinstance(hist, list) or not hist:
            return None
        last = hist[-1]
        return _first_number(last.get("close")) if isinstance(last, dict) else None

    def _check(old: dict, new: dict) -> tuple[bool, str]:
        if not isinstance(old, dict) or not isinstance(new, dict):
            return True, "ok"
        old_pairs = old.get("pairs") or {}
        for ticker, pair in (new.get("pairs") or {}).items():
            new_v = _latest_close(pair)
            old_v = _latest_close(old_pairs.get(ticker) or {})
            if old_v is None or new_v is None or old_v == 0:
                continue
            change = abs(new_v - old_v) / abs(old_v)
            if change > threshold:
                return False, (
                    f"{ticker} latest close swung {change * 100:.0f}% "
                    f"({old_v:g} → {new_v:g}), suspected parse/rescale error"
                )
        return True, "ok"

    return _check


def scalar_swing_guard(*path, threshold: float = 0.30):
    """Build an (old, new) -> (ok, reason) guard on one nested numeric field.

    Follows `path` (a sequence of keys) into both payloads and rejects a
    > `threshold` move of that scalar — e.g. `scalar_swing_guard("currency_index",
    "index_value")` catches a units/parse break in the CCI level written into
    quant_report.json, without touching the model-output sections that move by
    design. Missing/non-numeric on either side → skip (don't block).
    """
    def _dig(d):
        for k in path:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return _first_number(d)

    def _check(old: dict, new: dict) -> tuple[bool, str]:
        old_v, new_v = _dig(old), _dig(new)
        if old_v is None or new_v is None or old_v == 0:
            return True, "ok"
        change = abs(new_v - old_v) / abs(old_v)
        if change > threshold:
            return False, (
                f"{'.'.join(map(str, path))} swung {change * 100:.0f}% "
                f"({old_v:g} → {new_v:g}), suspected parse error"
            )
        return True, "ok"

    return _check


# ── validators ────────────────────────────────────────────────────────────────

def validate_futures_chain(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    for market in ("arabica", "robusta"):
        if data.get(market) is None:
            return False, f"missing {market}"
        contracts = data[market].get("contracts", [])
        if len(contracts) < 5:
            return False, f"{market} has {len(contracts)} contracts (need >= 5)"
        pub_date = data[market].get("pub_date")
        if pub_date and _days_since_date(pub_date) > 7:
            return False, f"{market} pub_date {pub_date} is > 7 days old"
    return True, "ok"


def validate_farmer_economics(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    if data.get("weather") is None:
        return False, "weather is null"
    items = (data.get("fertilizer") or {}).get("items", [])
    if not items:
        return False, "fertilizer.items is empty"
    scraped_at = data.get("scraped_at")
    if scraped_at and _days_since_iso(scraped_at) > 2:
        return False, f"scraped_at {scraped_at} is > 48 h old"
    return True, "ok"


def validate_cot(data: list) -> tuple[bool, str]:
    if not isinstance(data, list) or len(data) == 0:
        return False, "empty list"
    report_date = data[-1].get("date") or data[-1].get("report_date")
    if report_date and _days_since_date(report_date) > 14:
        return False, f"most recent date {report_date} is > 14 days old"
    return True, "ok"


def validate_cot_recent(data: list) -> tuple[bool, str]:
    if not isinstance(data, list) or len(data) == 0:
        return False, "empty list"
    return True, "ok"


def validate_health(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    if "scrapers" not in data:
        return False, "missing scrapers key"
    return True, "ok"


def validate_macro_cot(data: list) -> tuple[bool, str]:
    if not isinstance(data, list) or len(data) == 0:
        return False, "empty list"
    return True, "ok"


def validate_freight(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    if not data.get("routes"):
        return False, "routes list is empty"
    return True, "ok"


def validate_tender_parity(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    origins = data.get("origins")
    if not isinstance(origins, dict) or not origins:
        return False, "origins is empty"
    if not any((o.get("history") or []) for o in origins.values()):
        return False, "no origin has any history rows"
    return True, "ok"


def validate_cot_sept_study(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    years = data.get("years")
    if not isinstance(years, dict) or not years:
        return False, "years is empty"
    if not isinstance(data.get("current_year"), int):
        return False, "current_year missing"
    if not any(len(v.get("rows") or []) >= 3 for v in years.values()):
        return False, "no year has ≥3 weekly rows"
    return True, "ok"


def validate_yield_rainfall(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    models = data.get("models")
    if not isinstance(models, dict) or not models:
        return False, "models is empty"
    for k, m in models.items():
        if len(m.get("theoretical_curve") or []) < 4:
            return False, f"{k}: theoretical_curve too short"
        if len(m.get("historical_scatter") or []) < 10:
            return False, f"{k}: historical_scatter too short"
        if not isinstance(m.get("current_year_live"), dict):
            return False, f"{k}: current_year_live missing"
    return True, "ok"


def validate_cropyear_xray(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    markets = data.get("markets")
    if not isinstance(markets, dict) or not markets:
        return False, "markets is empty"
    if not any(
        sum(len(y.get("rows") or []) for y in (m.get("years") or {}).values()) >= 5
        for m in markets.values()
    ):
        return False, "no market has ≥5 weekly rows"
    return True, "ok"


def validate_oi_fnd_chart(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    for market in ("arabica", "robusta"):
        if market not in data:
            return False, f"missing {market}"
    return True, "ok"


def validate_oi_history(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    for market in ("arabica", "robusta"):
        rows = data.get(market) or []
        if not rows:
            return False, f"{market} rows are empty"
    return True, "ok"


def validate_quant_report(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    ci = data.get("currency_index")
    if not ci:
        return False, "currency_index is empty"

    # Staleness gate — if yfinance silently degrades (the 2026-05-11
    # onwards Yahoo Finance / GH Actions IP block produced empty-output
    # runs that still wrote a CCI section from the previous run via the
    # merge-into-existing logic), reject so the workflow's git checkout
    # fallback keeps the prior good file rather than committing stale data.
    scraped_at = ci.get("scraped_at") if isinstance(ci, dict) else None
    if not scraped_at:
        return False, "currency_index.scraped_at missing"
    try:
        from datetime import UTC, datetime, timedelta
        dt = datetime.fromisoformat(str(scraped_at).replace("Z", "+00:00"))
        age = datetime.now(UTC) - dt
        if age > timedelta(days=3):
            return False, f"currency_index.scraped_at is {age.days}d old"
    except Exception as e:
        return False, f"could not parse scraped_at ({e})"

    # currency_index.currencies must be non-empty and at least half the
    # 12 tracked pairs must have a non-null daily_chg — otherwise the
    # downstream sum would be biased by all the implicit zeros.
    currencies = ci.get("currencies") if isinstance(ci, dict) else None
    if not currencies or not isinstance(currencies, list):
        return False, "currency_index.currencies missing or wrong type"
    with_chg = sum(1 for c in currencies if c.get("daily_chg") is not None)
    if with_chg < len(currencies) // 2:
        return False, f"only {with_chg}/{len(currencies)} currencies have daily_chg"

    return True, "ok"


def validate_brazil_export_projection(data: dict) -> tuple[bool, str]:
    """Brazil daily SSOT forecast — emitted by scraper.brazil_export_forecast.

    Sanity-gate so a broken engine run can't blow up the three downstream
    front-end consumers (MonthlyVolume, CumulativePace, SupplyDemand)."""
    if not isinstance(data, dict):
        return False, "not a dict"
    curve = data.get("monthly_curve")
    if not isinstance(curve, list) or len(curve) != 12:
        return False, f"monthly_curve must be a 12-row list (got {len(curve) if isinstance(curve, list) else type(curve).__name__})"
    target = data.get("annual_target")
    if not isinstance(target, int) or target <= 0:
        return False, f"annual_target must be a positive int (got {target!r})"
    allowed = {"realized", "certificados", "seasonality"}
    for i, row in enumerate(curve):
        if not isinstance(row, dict):
            return False, f"row {i} not a dict"
        if row.get("status") not in allowed:
            return False, f"row {i} status={row.get('status')!r} not in {allowed}"
        if not isinstance(row.get("value"), int):
            return False, f"row {i} value must be int (got {row.get('value')!r})"
    # Curve should sum to the (possibly safeguard-adjusted) annual target.
    s = sum(r["value"] for r in curve)
    if abs(s - target) > 12:           # ≤ 1-bag rounding per month
        return False, f"curve sum {s} drifts > 12 bags from annual_target {target}"
    return True, "ok"


def validate_cecafe_daily(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    # v2 schema: per-source buckets under data["sources"]["embarques"|"certificados"].
    # v1 legacy: arabica/conillon/soluvel at the top level (came from the
    # Certificados de Origem table). Accept BOTH so the dual-source migration
    # doesn't get rejected and reverted by the workflow's validate step (which
    # was the bug that kept embarques from ever committing).
    sources = data.get("sources")
    if isinstance(sources, dict):
        # Valid if ANY source carries arabica or conillon data.
        for src_name, bucket in sources.items():
            if not isinstance(bucket, dict):
                continue
            if (bucket.get("arabica") or {}) or (bucket.get("conillon") or {}):
                return True, f"ok (v2, source={src_name})"
        return False, "v2 schema but no source has arabica/conillon data"
    # Legacy fallback.
    arabica = data.get("arabica") or {}
    conillon = data.get("conillon") or {}
    if not arabica and not conillon:
        return False, "no arabica or conillon data"
    return True, "ok (v1 legacy)"


def validate_earnings(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    if not data.get("companies"):
        return False, "companies list is empty"
    return True, "ok"


def validate_kaffeesteuer(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    if len(data) == 0:
        return False, "empty dict — no monthly records"
    return True, "ok"


# ── write helper ──────────────────────────────────────────────────────────────

def safe_write_json(path, payload, validate_fn=None, indent: int = 2, sanity_fn=None,
                    ensure_ascii: bool = True, separators=None,
                    sort_keys: bool = False, trailing_newline: bool = False) -> bool:
    """
    Validate payload then write to path atomically via a .tmp file.

    `validate_fn(payload) -> (ok, reason)` checks the new payload in isolation
    (shape, freshness). Pass None to write atomically WITHOUT a shape gate — use
    this to get crash-safe (tmp-then-rename) writes on a file that doesn't yet
    have a bespoke validator; add one later. `sanity_fn(old_payload, new_payload)
    -> (ok, reason)` is an optional cross-run guard compared against the last good
    file (e.g. a volatility/price-swing check); it is skipped on the first write.

    `ensure_ascii`, `separators`, `sort_keys` mirror json.dumps, and
    `trailing_newline` appends a final "\\n", so a caller migrating off a raw
    `write_text(json.dumps(...))` can preserve its exact on-disk format. Defaults
    match the original behaviour so existing callers are unaffected.

    Returns True if written, False if validation/sanity failed or the content
    is unchanged. On failure the existing file at `path` is left untouched.
    """
    if validate_fn is not None:
        ok, reason = validate_fn(payload)
        if not ok:
            name = Path(path).name
            print(f"[validate] {name} FAILED: {reason} — keeping existing file")
            return False

    serialized = json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii,
                            separators=separators, sort_keys=sort_keys)
    if trailing_newline:
        serialized += "\n"

    # Read the existing file once (as text) — reused by the sanity guard and the
    # content short-circuit below.
    p = Path(path)
    existing = None
    if p.exists():
        try:
            existing = p.read_text(encoding="utf-8")
        except Exception:
            existing = None  # unreadable/corrupt → treat as no prior

    # Cross-run sanity guard (volatility). It needs the parsed prior payload, so
    # we only pay the json.loads cost when a guard is actually supplied — not on
    # every one of the ~21 files. Skipped on the first-ever write.
    if sanity_fn is not None and existing is not None:
        try:
            old_payload = json.loads(existing)
        except Exception:
            old_payload = None
        if old_payload is not None:
            ok2, reason2 = sanity_fn(old_payload, payload)
            if not ok2:
                print(f"[validate] {Path(path).name} SANITY FAILED: {reason2} — keeping existing file")
                return False

    # Content short-circuit: these files are always written as
    # json.dumps(payload, indent=indent), so comparing the on-disk text to the
    # freshly-serialized string is exact and skips a CPU-heavy JSON re-parse.
    # Makes the export idempotent and avoids pointless .tmp churn.
    if existing == serialized:
        return False

    tmp = Path(str(path) + ".tmp")
    tmp.write_text(serialized, encoding="utf-8")
    tmp.replace(path)
    return True
