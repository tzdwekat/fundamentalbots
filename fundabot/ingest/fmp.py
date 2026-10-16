"""Financial Modeling Prep: annual consensus estimates, earnings surprises, price-target consensus.

Vendor data, not filings. Stored raw per fetch so estimate revisions can be computed from history.
"""
from datetime import date, datetime, timezone

import httpx
import pandas as pd
import psycopg

from fundabot.config import settings
from fundabot.db.session import execute_values

BASE = "https://financialmodelingprep.com/stable"


class FMPError(Exception):
    pass


def get(path: str, **params) -> list | dict:
    r = httpx.get(f"{BASE}/{path}", params={**params, "apikey": settings.fmp_api_key}, timeout=30)
    if r.status_code != 200 or r.text.startswith(("Restricted Endpoint", "Premium Query Parameter")):
        raise FMPError(f"{path}: {r.status_code} {r.text[:120]}")
    return r.json()


def _last_two_quarters(today: date | None = None) -> list[tuple[int, int]]:
    d = today or date.today()
    q = (d.month - 1) // 3 + 1  # current calendar quarter; 13F data lags, so ask for the two completed ones
    out = []
    y, qq = d.year, q
    for _ in range(2):
        qq -= 1
        if qq == 0:
            y, qq = y - 1, 4
        out.append((y, qq))
    return out


def fetch_all(ticker: str) -> dict:
    t = ticker.upper().replace("-", ".")  # FMP uses BRK.B style
    inst = []
    for y, q in _last_two_quarters():
        try:
            inst += get("institutional-ownership/symbol-positions-summary", symbol=t, year=y, quarter=q)
        except FMPError:
            pass
    return {
        "estimates_annual": get("analyst-estimates", symbol=t, period="annual", limit=5),
        "estimates_quarter": get("analyst-estimates", symbol=t, period="quarter", limit=8),
        "earnings": get("earnings", symbol=t),
        "price_target": get("price-target-consensus", symbol=t),
        "institutional": inst,
        "insider_stats": get("insider-trading/statistics", symbol=t),
    }


def _d(s):
    return date.fromisoformat(s[:10]) if s else None


def parse_estimates(rows: list[dict]) -> list[dict]:
    return [{"period_end": _d(r["date"]), "revenue_avg": r.get("revenueAvg"), "revenue_low": r.get("revenueLow"), "revenue_high": r.get("revenueHigh"),
             "eps_avg": r.get("epsAvg"), "eps_low": r.get("epsLow"), "eps_high": r.get("epsHigh"), "analysts": int(r.get("numAnalystsEps") or 0)}
            for r in rows if r.get("date")]


def parse_surprises(rows: list[dict]) -> list[dict]:
    return [{"report_date": _d(r["date"]), "eps_actual": r.get("epsActual"), "eps_estimated": r.get("epsEstimated"),
             "revenue_actual": r.get("revenueActual"), "revenue_estimated": r.get("revenueEstimated")}
            for r in rows if r.get("date") and r.get("epsActual") is not None]


def parse_price_target(rows: list | dict) -> dict | None:
    r = rows[0] if isinstance(rows, list) and rows else rows if isinstance(rows, dict) else None
    if not r or r.get("targetConsensus") is None:
        return None
    return {"consensus": float(r["targetConsensus"]), "median": float(r["targetMedian"]), "high": float(r["targetHigh"]), "low": float(r["targetLow"])}


def ingest(conn: psycopg.Connection, cik: int, ticker: str, fetched_at: datetime | None = None) -> datetime:
    raw = fetch_all(ticker)
    fetched_at = fetched_at or datetime.now(timezone.utc)
    for period, key in (("annual", "estimates_annual"), ("quarter", "estimates_quarter")):
        execute_values(conn, "INSERT INTO estimates (cik, fetched_at, period, period_end, revenue_avg, revenue_low, revenue_high, eps_avg, eps_low, eps_high, analysts) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       [(cik, fetched_at, period, e["period_end"], e["revenue_avg"], e["revenue_low"], e["revenue_high"], e["eps_avg"], e["eps_low"], e["eps_high"], e["analysts"])
                        for e in parse_estimates(raw.get(key) or [])])
    execute_values(conn, "INSERT INTO institutional (cik, quarter_end, holders, holders_change, shares, shares_change, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                   "ON CONFLICT (cik, quarter_end) DO UPDATE SET holders = EXCLUDED.holders, holders_change = EXCLUDED.holders_change, shares = EXCLUDED.shares, "
                   "shares_change = EXCLUDED.shares_change, fetched_at = EXCLUDED.fetched_at",
                   [(cik, _d(r["date"]), r.get("investorsHolding"), r.get("investorsHoldingChange"), r.get("numberOf13Fshares"), r.get("numberOf13FsharesChange"), fetched_at)
                    for r in raw.get("institutional") or [] if r.get("date")])
    execute_values(conn, "INSERT INTO insiders (cik, year, quarter, acquired, disposed, purchases, sales, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                   "ON CONFLICT (cik, year, quarter) DO UPDATE SET acquired = EXCLUDED.acquired, disposed = EXCLUDED.disposed, purchases = EXCLUDED.purchases, "
                   "sales = EXCLUDED.sales, fetched_at = EXCLUDED.fetched_at",
                   [(cik, int(r["year"]), int(r["quarter"]), r.get("totalAcquired"), r.get("totalDisposed"), r.get("totalPurchases"), r.get("totalSales"), fetched_at)
                    for r in raw.get("insider_stats") or [] if r.get("year")])
    execute_values(conn, "INSERT INTO surprises (cik, report_date, eps_actual, eps_estimated, revenue_actual, revenue_estimated) VALUES (%s,%s,%s,%s,%s,%s) "
                   "ON CONFLICT (cik, report_date) DO UPDATE SET eps_actual = EXCLUDED.eps_actual, eps_estimated = EXCLUDED.eps_estimated, "
                   "revenue_actual = EXCLUDED.revenue_actual, revenue_estimated = EXCLUDED.revenue_estimated",
                   [(cik, s["report_date"], s["eps_actual"], s["eps_estimated"], s["revenue_actual"], s["revenue_estimated"]) for s in parse_surprises(raw["earnings"])[:12]])
    pt = parse_price_target(raw["price_target"])
    if pt:
        execute_values(conn, "INSERT INTO price_targets (cik, fetched_at, consensus, median, high, low) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       [(cik, fetched_at, pt["consensus"], pt["median"], pt["high"], pt["low"])])
    return fetched_at


def load(conn: psycopg.Connection, cik: int) -> dict:
    def q(sql, cols):
        with conn.cursor() as cur:
            cur.execute(sql, (cik,))
            df = pd.DataFrame(cur.fetchall(), columns=cols)
        for c in df.columns:
            if c not in ("fetched_at", "period", "period_end", "report_date", "quarter_end"):
                df[c] = pd.to_numeric(df[c])  # numeric columns arrive as Decimal
        return df
    est = q("SELECT fetched_at, period, period_end, revenue_avg, revenue_low, revenue_high, eps_avg, eps_low, eps_high, analysts FROM estimates WHERE cik = %s ORDER BY fetched_at, period_end",
            ["fetched_at", "period", "period_end", "revenue_avg", "revenue_low", "revenue_high", "eps_avg", "eps_low", "eps_high", "analysts"])
    sur = q("SELECT report_date, eps_actual, eps_estimated, revenue_actual, revenue_estimated FROM surprises WHERE cik = %s ORDER BY report_date",
            ["report_date", "eps_actual", "eps_estimated", "revenue_actual", "revenue_estimated"])
    pt = q("SELECT fetched_at, consensus, median, high, low FROM price_targets WHERE cik = %s ORDER BY fetched_at", ["fetched_at", "consensus", "median", "high", "low"])
    inst = q("SELECT quarter_end, holders, holders_change, shares, shares_change FROM institutional WHERE cik = %s ORDER BY quarter_end",
             ["quarter_end", "holders", "holders_change", "shares", "shares_change"])
    ins = q("SELECT year, quarter, acquired, disposed, purchases, sales FROM insiders WHERE cik = %s ORDER BY year, quarter",
            ["year", "quarter", "acquired", "disposed", "purchases", "sales"])
    return {"estimates": est, "surprises": sur, "price_targets": pt, "institutional": inst, "insiders": ins}


def fetch_transcript_dates(ticker: str) -> list[dict]:
    """[{quarter, fiscalYear, date}], newest first, in the company's own fiscal convention."""
    return get("earning-call-transcript-dates", symbol=ticker.upper().replace("-", "."))


def fetch_transcript(ticker: str, year: int, quarter: int) -> dict | None:
    rows = get("earning-call-transcript", symbol=ticker.upper().replace("-", "."), year=year, quarter=quarter)
    return rows[0] if rows else None
