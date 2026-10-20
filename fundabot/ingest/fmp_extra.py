"""The rest of the decision-relevant FMP data: profile, peers with multiples, revenue segments, dividends, daily volume,
insider trades, top holders, and small facts (TTM ratios, scores, ratings, 10-year yield, batch quotes) cached in
ticker_facts. Every dataset has fetched_at and a refresh rule; a failed refresh keeps stored rows."""
import logging
import re
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from fundabot.ingest import fmp

log = logging.getLogger("fundabot")
US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]*(\.[A-Z])?$")  # BRK.B stays; 066570.KS / 002241.SZ go
MAX_PEERS = 8


def _sym(ticker: str) -> str:
    return ticker.upper().replace("-", ".")


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _fresh(conn, table: str, cik: int, max_age_hours: float) -> bool:
    """Fresh when rows were fetched recently OR an attempt recently came back empty (so a non-payer is not refetched on every page view)."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT max(fetched_at) FROM {table} WHERE cik = %s", (cik,))
        last = cur.fetchone()[0]
        cur.execute("SELECT fetched_at FROM ticker_facts WHERE cik = %s AND key = %s", (cik, f"fetched:{table}"))
        attempt = cur.fetchone()
    stamps = [t for t in (last, attempt[0] if attempt else None) if t is not None]
    return bool(stamps) and datetime.now(timezone.utc) - max(stamps) < timedelta(hours=max_age_hours)


def _attempted(conn, table: str, cik: int) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ticker_facts (cik, key, value, fetched_at) VALUES (%s, %s, 'null', now()) ON CONFLICT (cik, key) DO UPDATE SET fetched_at = now()", (cik, f"fetched:{table}"))
    conn.commit()


# ---- profile
def fetch_profile(ticker: str, get=fmp.get) -> dict | None:
    rows = get("profile", symbol=_sym(ticker))
    if not rows:
        return None
    r = rows[0]
    return {"name": r.get("companyName"), "sector": r.get("sector"), "industry": r.get("industry"), "exchange": r.get("exchange"), "description": r.get("description"),
            "employees": int(r["fullTimeEmployees"]) if r.get("fullTimeEmployees") not in (None, "") else None, "beta": _num(r.get("beta")),
            "ipo_date": date.fromisoformat(r["ipoDate"]) if r.get("ipoDate") else None}


def ingest_profile(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, max_age_hours: float = 24) -> bool:
    if _fresh(conn, "company_profile", cik, max_age_hours):
        return False
    p = fetch_profile(ticker, get)
    _attempted(conn, "company_profile", cik)
    if p is None:
        return False
    with conn.cursor() as cur:
        cur.execute("INSERT INTO company_profile (cik, name, sector, industry, exchange, description, employees, beta, ipo_date, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) "
                    "ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name, sector = EXCLUDED.sector, industry = EXCLUDED.industry, exchange = EXCLUDED.exchange, description = EXCLUDED.description, "
                    "employees = EXCLUDED.employees, beta = EXCLUDED.beta, ipo_date = EXCLUDED.ipo_date, fetched_at = now()",
                    (cik, p["name"], p["sector"], p["industry"], p["exchange"], p["description"], p["employees"], p["beta"], p["ipo_date"]))
    conn.commit()
    return True


def load_profile(conn: psycopg.Connection, cik: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT name, sector, industry, exchange, description, employees, beta, ipo_date, fetched_at FROM company_profile WHERE cik = %s", (cik,))
        r = cur.fetchone()
    if r is None:
        return None
    d = dict(zip(["name", "sector", "industry", "exchange", "description", "employees", "beta", "ipo_date", "fetched_at"], r))
    d["beta"] = _num(d["beta"])
    return d


# ---- peers
def us_peers(rows: list[dict], own: str) -> list[str]:
    out = [r["symbol"] for r in rows if US_SYMBOL.match(r.get("symbol") or "") and r["symbol"] != _sym(own)]
    return out[:MAX_PEERS]


def ingest_peers(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, max_age_hours: float = 24) -> int:
    if _fresh(conn, "peers", cik, max_age_hours):
        return 0
    names = us_peers(get("stock-peers", symbol=_sym(ticker)), ticker)
    _attempted(conn, "peers", cik)
    quotes = batch_quotes(names, get) if names else {}
    n = 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM peers WHERE cik = %s", (cik,))
        for peer in names:
            try:
                ratios = (get("ratios-ttm", symbol=peer) or [{}])[0]
                km = (get("key-metrics-ttm", symbol=peer) or [{}])[0]
            except fmp.FMPError as e:
                log.warning("peer %s multiples unavailable: %s", peer, e)
                ratios, km = {}, {}
            q = quotes.get(peer, {})
            cur.execute("INSERT INTO peers (cik, peer, name, market_cap, price, year_high, year_low, pe_ttm, ev_sales_ttm, ev_ebitda_ttm) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (cik, peer, q.get("name"), q.get("market_cap"), q.get("price"), q.get("year_high"), q.get("year_low"),
                         _num(ratios.get("priceToEarningsRatioTTM")), _num(km.get("evToSalesTTM")), _num(km.get("evToEBITDATTM"))))
            n += 1
    conn.commit()
    return n


def load_peers(conn: psycopg.Connection, cik: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT peer, name, market_cap, price, year_high, year_low, pe_ttm, ev_sales_ttm, ev_ebitda_ttm FROM peers WHERE cik = %s ORDER BY market_cap DESC NULLS LAST, peer", (cik,))
        cols = ["peer", "name", "market_cap", "price", "year_high", "year_low", "pe_ttm", "ev_sales_ttm", "ev_ebitda_ttm"]
        return [{k: (_num(v) if k not in ("peer", "name") else v) for k, v in zip(cols, r)} for r in cur.fetchall()]


# ---- segments
def ingest_segments(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, max_age_hours: float = 24) -> int:
    if _fresh(conn, "segments", cik, max_age_hours):
        return 0
    n = 0
    _attempted(conn, "segments", cik)
    with conn.cursor() as cur:
        for path, kind in (("revenue-product-segmentation", "product"), ("revenue-geographic-segmentation", "geographic")):
            for row in get(path, symbol=_sym(ticker), period="annual"):
                fy = int(row["fiscalYear"])
                for name, revenue in (row.get("data") or {}).items():
                    cur.execute("INSERT INTO segments (cik, kind, fiscal_year, name, revenue, fetched_at) VALUES (%s,%s,%s,%s,%s,now()) "
                                "ON CONFLICT (cik, kind, fiscal_year, name) DO UPDATE SET revenue = EXCLUDED.revenue, fetched_at = now()", (cik, kind, fy, name, _num(revenue)))
                    n += 1
    conn.commit()
    return n


def load_segments(conn: psycopg.Connection, cik: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT kind, fiscal_year, name, revenue FROM segments WHERE cik = %s ORDER BY kind, fiscal_year, revenue DESC", (cik,))
        return [{"kind": k, "fiscal_year": fy, "name": name, "revenue": _num(rev)} for k, fy, name, rev in cur.fetchall()]


# ---- dividends
def ingest_dividends(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, max_age_hours: float = 24, limit: int = 40) -> int:
    if _fresh(conn, "dividends", cik, max_age_hours):
        return 0
    n = 0
    _attempted(conn, "dividends", cik)
    with conn.cursor() as cur:
        for r in get("dividends", symbol=_sym(ticker), limit=limit):
            cur.execute("INSERT INTO dividends (cik, ex_date, payment_date, declaration_date, amount, adj_amount, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,now()) "
                        "ON CONFLICT (cik, ex_date) DO UPDATE SET amount = EXCLUDED.amount, adj_amount = EXCLUDED.adj_amount, payment_date = EXCLUDED.payment_date, fetched_at = now()",
                        (cik, date.fromisoformat(r["date"]), date.fromisoformat(r["paymentDate"]) if r.get("paymentDate") else None,
                         date.fromisoformat(r["declarationDate"]) if r.get("declarationDate") else None, _num(r.get("dividend")), _num(r.get("adjDividend"))))
            n += 1
    conn.commit()
    return n


def load_dividends(conn: psycopg.Connection, cik: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT ex_date, payment_date, declaration_date, amount, adj_amount FROM dividends WHERE cik = %s ORDER BY ex_date DESC", (cik,))
        return [{"ex_date": a, "payment_date": b, "declaration_date": c, "amount": _num(d), "adj_amount": _num(e)} for a, b, c, d, e in cur.fetchall()]


# ---- volume (closes stay yfinance's; volume joins by date)
def ingest_volume(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, days: int = 400, today: date | None = None) -> int:
    today = today or date.today()
    rows = get("historical-price-eod/full", symbol=_sym(ticker), **{"from": (today - timedelta(days=days)).isoformat(), "to": today.isoformat()})
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            if r.get("volume") is None:
                continue
            cur.execute("UPDATE prices SET volume = %s WHERE cik = %s AND date = %s", (int(r["volume"]), cik, date.fromisoformat(r["date"])))
            n += cur.rowcount
    conn.commit()
    return n


# ---- insider trades and holders
def ingest_insider_trades(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, max_age_hours: float = 6, limit: int = 20) -> int:
    if _fresh(conn, "insider_trades", cik, max_age_hours):
        return 0
    n = 0
    _attempted(conn, "insider_trades", cik)
    with conn.cursor() as cur:
        for r in get("insider-trading/search", symbol=_sym(ticker), limit=limit):
            cur.execute("INSERT INTO insider_trades (cik, filing_date, transaction_date, reporting_name, role, transaction_type, acquisition, shares, price, url, fetched_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                        (cik, date.fromisoformat(r["filingDate"][:10]), date.fromisoformat(r["transactionDate"][:10]) if r.get("transactionDate") else None, r["reportingName"],
                         r.get("typeOfOwner"), r.get("transactionType"), r.get("acquisitionOrDisposition"), _num(r.get("securitiesTransacted")), _num(r.get("price")), r.get("url")))
            n += cur.rowcount
        cur.execute("UPDATE insider_trades SET fetched_at = now() WHERE cik = %s", (cik,))
    conn.commit()
    return n


def load_insider_trades(conn: psycopg.Connection, cik: int, limit: int = 20) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT filing_date, transaction_date, reporting_name, role, transaction_type, acquisition, shares, price, url FROM insider_trades WHERE cik = %s "
                    "ORDER BY filing_date DESC, transaction_date DESC LIMIT %s", (cik, limit))
        cols = ["filing_date", "transaction_date", "reporting_name", "role", "transaction_type", "acquisition", "shares", "price", "url"]
        return [{k: (_num(v) if k in ("shares", "price") else v) for k, v in zip(cols, r)} for r in cur.fetchall()]


def ingest_holders(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, max_age_hours: float = 24, today: date | None = None, limit: int = 10) -> int:
    if _fresh(conn, "holders", cik, max_age_hours):
        return 0
    rows = []
    today = today or date.today()
    for y, q in fmp._last_two_quarters(today):
        quarter_end = date(y, 3 * q, 1) + timedelta(days=31)
        quarter_end = quarter_end.replace(day=1) - timedelta(days=1)
        if (today - quarter_end).days < 60:  # 13Fs are due 45 days after quarter end; before that only early filers show
            continue
        try:
            rows = get("institutional-ownership/extract-analytics/holder", symbol=_sym(ticker), year=y, quarter=q, limit=limit)
        except fmp.FMPError:
            rows = []
        if rows:
            break
    n = 0
    _attempted(conn, "holders", cik)
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("INSERT INTO holders (cik, quarter_end, investor, shares, market_value, change_pct, ownership_pct, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,now()) "
                        "ON CONFLICT (cik, quarter_end, investor) DO UPDATE SET shares = EXCLUDED.shares, market_value = EXCLUDED.market_value, change_pct = EXCLUDED.change_pct, ownership_pct = EXCLUDED.ownership_pct, fetched_at = now()",
                        (cik, date.fromisoformat(r["date"]), r["investorName"], _num(r.get("sharesNumber")), _num(r.get("marketValue")), _num(r.get("changeInSharesNumberPercentage")), _num(r.get("ownership"))))
            n += 1
    conn.commit()
    return n


def load_holders(conn: psycopg.Connection, cik: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT quarter_end, investor, shares, market_value, change_pct, ownership_pct FROM holders WHERE cik = %s AND quarter_end = (SELECT max(quarter_end) FROM holders WHERE cik = %s) "
                    "ORDER BY market_value DESC NULLS LAST", (cik, cik))
        cols = ["quarter_end", "investor", "shares", "market_value", "change_pct", "ownership_pct"]
        return [{k: (_num(v) if k not in ("quarter_end", "investor") else v) for k, v in zip(cols, r)} for r in cur.fetchall()]


# ---- financial statements (the primary source of the fundamentals series since Phase 5d)
STATEMENTS = (("income", "income-statement"), ("balance", "balance-sheet-statement"), ("cashflow", "cash-flow-statement"))


def fetch_statements(ticker: str, get=fmp.get, annual_limit: int = 12, quarter_limit: int = 24) -> dict[str, list[dict]]:
    """Six lists keyed <statement>_<annual|quarter>; a statement that fails is logged and left empty; all failing raises."""
    out, failures = {}, []
    for key, path in STATEMENTS:
        for period_type, limit in (("annual", annual_limit), ("quarter", quarter_limit)):
            try:
                out[f"{key}_{period_type}"] = get(path, symbol=_sym(ticker), period=period_type, limit=limit) or []
            except fmp.FMPError as e:
                log.warning("%s %s %s unavailable: %s", ticker, path, period_type, e)
                out[f"{key}_{period_type}"] = []
                failures.append(path)
    if len(failures) == len(STATEMENTS) * 2:
        raise fmp.FMPError(f"{ticker}: every statement endpoint failed")
    return out


def ingest_statements(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, max_age_hours: float = 24, force: bool = False) -> int:
    if not force and _fresh(conn, "fmp_statements", cik, max_age_hours):
        return 0
    data = fetch_statements(ticker, get)
    _attempted(conn, "fmp_statements", cik)
    n = 0
    with conn.cursor() as cur:
        for key, rows in data.items():
            statement, period_type = key.split("_")
            for r in rows:
                if not r.get("date") or r.get("fiscalYear") in (None, ""):
                    continue
                cur.execute("INSERT INTO fmp_statements (cik, statement, period_type, fiscal_year, period, date, filing_date, data, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) "
                            "ON CONFLICT (cik, statement, period_type, fiscal_year, period) DO UPDATE SET date = EXCLUDED.date, filing_date = EXCLUDED.filing_date, data = EXCLUDED.data, fetched_at = now()",
                            (cik, statement, period_type, int(r["fiscalYear"]), r["period"], date.fromisoformat(r["date"][:10]), date.fromisoformat(r["filingDate"][:10]) if r.get("filingDate") else None, Jsonb(r)))
                n += 1
    conn.commit()
    return n


def load_statements(conn: psycopg.Connection, cik: int) -> dict[str, list[dict]]:
    out = {f"{k}_{pt}": [] for k, _ in STATEMENTS for pt in ("annual", "quarter")}
    with conn.cursor() as cur:
        cur.execute("SELECT statement, period_type, fiscal_year, period, date, filing_date, data FROM fmp_statements WHERE cik = %s ORDER BY date DESC, period DESC", (cik,))
        for statement, period_type, fy, period, d, fd, data in cur.fetchall():
            out[f"{statement}_{period_type}"].append({**data, "fiscal_year": fy, "period": period, "date": d, "filing_date": fd})
    return out


# ---- small facts, cached in ticker_facts
def fact(conn: psycopg.Connection, cik: int, key: str, loader, max_age_hours: float):
    """Cached loader result; a loader failure logs and returns the stored value (or None)."""
    with conn.cursor() as cur:
        cur.execute("SELECT value, fetched_at FROM ticker_facts WHERE cik = %s AND key = %s", (cik, key))
        row = cur.fetchone()
    if row and datetime.now(timezone.utc) - row[1] < timedelta(hours=max_age_hours):
        return row[0]
    try:
        value = loader()
    except Exception as e:
        log.warning("fact %s for cik %s failed: %s", key, cik, e)
        return row[0] if row else None
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ticker_facts (cik, key, value, fetched_at) VALUES (%s,%s,%s,now()) ON CONFLICT (cik, key) DO UPDATE SET value = EXCLUDED.value, fetched_at = now()", (cik, key, Jsonb(value)))
    conn.commit()
    return value


def fetch_ttm(ticker: str, get=fmp.get) -> dict:
    out = {}
    for path in ("ratios-ttm", "key-metrics-ttm"):
        rows = get(path, symbol=_sym(ticker))
        if rows:
            out.update({k: _num(v) if k not in ("symbol",) else v for k, v in rows[0].items()})
    return out


def fetch_scores(ticker: str, get=fmp.get) -> dict | None:
    rows = get("financial-scores", symbol=_sym(ticker))
    if not rows:
        return None
    r = rows[0]
    return {"altman_z": _num(r.get("altmanZScore")), "piotroski": int(r["piotroskiScore"]) if r.get("piotroskiScore") not in (None, "") else None}


def fetch_ratings(ticker: str, get=fmp.get) -> dict | None:
    rows = get("grades-historical", symbol=_sym(ticker), limit=1)
    if not rows:
        return None
    r = rows[0]
    return {"date": r["date"], "strong_buy": int(r["analystRatingsStrongBuy"]), "buy": int(r["analystRatingsBuy"]), "hold": int(r["analystRatingsHold"]),
            "sell": int(r["analystRatingsSell"]), "strong_sell": int(r["analystRatingsStrongSell"])}


def fetch_treasury_10y(get=fmp.get, today: date | None = None) -> tuple[date, float] | None:
    today = today or date.today()
    rows = get("treasury-rates", **{"from": (today - timedelta(days=10)).isoformat(), "to": today.isoformat()})
    rows = sorted((r for r in rows if r.get("year10") not in (None, "")), key=lambda r: r["date"])
    return (date.fromisoformat(rows[-1]["date"]), float(rows[-1]["year10"])) if rows else None


def batch_quotes(tickers: list[str], get=fmp.get) -> dict[str, dict]:
    if not tickers:
        return {}
    out = {}
    for r in get("batch-quote", symbols=",".join(_sym(t) for t in tickers)):
        out[r["symbol"]] = {"name": r.get("name"), "price": _num(r.get("price")), "change_pct": _num(r.get("changePercentage")), "market_cap": _num(r.get("marketCap")),
                            "year_high": _num(r.get("yearHigh")), "year_low": _num(r.get("yearLow")), "volume": _num(r.get("volume")), "timestamp": r.get("timestamp")}
    return out
