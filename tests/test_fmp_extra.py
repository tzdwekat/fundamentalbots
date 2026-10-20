"""FMP breadth ingest: every dataset stored with its refresh rule; fixtures captured 2026-10-19."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from fundabot.ingest import fmp_extra

FIX = Path(__file__).parent / "fixtures"


def fx(name):
    return json.loads((FIX / f"fmp_aapl_{name}.json").read_text())


def fake_get(path, **params):
    sym = params.get("symbol", "")
    table = {"profile": "profile", "stock-peers": "peers", "revenue-product-segmentation": "seg_product", "revenue-geographic-segmentation": "seg_geo",
             "dividends": "dividends", "historical-price-eod/full": "eod", "insider-trading/search": "insider_trades",
             "institutional-ownership/extract-analytics/holder": "holders", "batch-quote": "batch_quote", "treasury-rates": "treasury",
             "financial-scores": "scores", "grades-historical": "grades_hist"}
    if path in ("ratios-ttm", "key-metrics-ttm"):
        return fx(("peer_ratios_msft" if path == "ratios-ttm" else "peer_km_msft") if sym != "AAPL" else ("ratios_ttm" if path == "ratios-ttm" else "key_metrics_ttm"))
    return fx(table[path])


@pytest.mark.db
def test_profile_roundtrip(db_conn):
    assert fmp_extra.ingest_profile(db_conn, 320193, "AAPL", get=fake_get) is True
    assert fmp_extra.ingest_profile(db_conn, 320193, "AAPL", get=fake_get) is False  # fresh
    p = fmp_extra.load_profile(db_conn, 320193)
    assert p["name"] == "Apple Inc." and p["sector"] == "Technology" and p["industry"] == "Consumer Electronics" and p["employees"] == 166000 and p["beta"] == pytest.approx(1.069)
    assert fmp_extra.load_profile(db_conn, 1) is None


def test_us_peers_filter():
    assert fmp_extra.us_peers(fx("peers"), "AAPL") == ["PCRFF", "SONY", "SSNLF", "XIACF"]
    rows = [{"symbol": "BRK.B"}, {"symbol": "066570.KS"}, {"symbol": "AAPL"}, {"symbol": "MSFT"}] + [{"symbol": f"P{i}"} for i in range(10)]
    out = fmp_extra.us_peers(rows, "AAPL")
    assert "BRK.B" in out and "066570.KS" not in out and "AAPL" not in out and len(out) == 8


@pytest.mark.db
def test_ingest_peers_stores_multiples(db_conn):
    assert fmp_extra.ingest_peers(db_conn, 320193, "AAPL", get=fake_get) == 4
    rows = fmp_extra.load_peers(db_conn, 320193)
    assert [r["peer"] for r in rows] == ["PCRFF", "SONY", "SSNLF", "XIACF"]
    sony = next(r for r in rows if r["peer"] == "SONY")
    assert sony["pe_ttm"] == pytest.approx(fx("peer_ratios_msft")[0]["priceToEarningsRatioTTM"]) and sony["ev_sales_ttm"] == pytest.approx(fx("peer_km_msft")[0]["evToSalesTTM"])
    assert fmp_extra.ingest_peers(db_conn, 320193, "AAPL", get=fake_get) == 0  # fresh


@pytest.mark.db
def test_segments_both_kinds_latest_years(db_conn):
    n = fmp_extra.ingest_segments(db_conn, 320193, "AAPL", get=fake_get)
    rows = fmp_extra.load_segments(db_conn, 320193)
    assert n == len(rows) > 20 and {r["kind"] for r in rows} == {"product", "geographic"}
    iphone = [r for r in rows if r["kind"] == "product" and r["name"] == "iPhone"]
    assert max(r["fiscal_year"] for r in iphone) == 2025 and next(r for r in iphone if r["fiscal_year"] == 2025)["revenue"] == 209586000000


@pytest.mark.db
def test_dividends_desc(db_conn):
    assert fmp_extra.ingest_dividends(db_conn, 320193, "AAPL", get=fake_get) == 40
    rows = fmp_extra.load_dividends(db_conn, 320193)
    assert rows[0]["ex_date"] == date(2026, 8, 10) and rows[0]["amount"] == pytest.approx(0.27) and rows[0]["ex_date"] > rows[1]["ex_date"]


@pytest.mark.db
def test_volume_join_keeps_closes(db_conn):
    eod = fx("eod")
    with db_conn.cursor() as cur:
        for r in eod[:3]:
            cur.execute("INSERT INTO prices (cik, date, close) VALUES (320193, %s, 1.0)", (r["date"],))
    db_conn.commit()
    assert fmp_extra.ingest_volume(db_conn, 320193, "AAPL", get=fake_get) == 3
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), sum(volume), min(close) FROM prices WHERE cik = 320193")
        n, vol, close = cur.fetchone()
    assert n == 3 and vol == sum(r["volume"] for r in eod[:3]) and float(close) == 1.0


@pytest.mark.db
def test_insider_trades_and_holders(db_conn):
    assert fmp_extra.ingest_insider_trades(db_conn, 320193, "AAPL", get=fake_get) > 0
    t = fmp_extra.load_insider_trades(db_conn, 320193, limit=5)
    assert len(t) == 5 and t[0]["reporting_name"] and t[0]["filing_date"] >= t[-1]["filing_date"] and t[0]["url"].startswith("https://www.sec.gov")
    assert fmp_extra.ingest_holders(db_conn, 320193, "AAPL", get=fake_get, today=date(2026, 10, 19)) == 10
    h = fmp_extra.load_holders(db_conn, 320193)
    assert h[0]["investor"] == "BLACKROCK, INC." and h[0]["market_value"] > h[1]["market_value"] and h[0]["quarter_end"] == date(2026, 6, 30) and h[0]["ownership_pct"] == pytest.approx(7.9156)


@pytest.mark.db
def test_fact_cache_respects_age(db_conn):
    calls = []

    def loader():
        calls.append(1)
        return {"v": len(calls)}
    assert fmp_extra.fact(db_conn, 320193, "k", loader, max_age_hours=6) == {"v": 1}
    assert fmp_extra.fact(db_conn, 320193, "k", loader, max_age_hours=6) == {"v": 1} and len(calls) == 1
    assert fmp_extra.fact(db_conn, 320193, "k", loader, max_age_hours=0) == {"v": 2}
    assert fmp_extra.fact(db_conn, 320193, "boom", lambda: (_ for _ in ()).throw(RuntimeError("x")), max_age_hours=6) is None  # failure: None, not a crash


def test_batch_quotes_and_facts_shapes():
    q = fmp_extra.batch_quotes(["AAPL", "NVDA"], get=fake_get)
    assert set(q) == {"AAPL", "NVDA"} and q["AAPL"]["price"] == pytest.approx(342.94) and q["AAPL"]["change_pct"] == pytest.approx(-0.1514) and q["AAPL"]["year_high"] == pytest.approx(348.15)
    ttm = fmp_extra.fetch_ttm("AAPL", get=fake_get)
    assert ttm["priceToEarningsRatioTTM"] == pytest.approx(39.148, rel=1e-3) and ttm["evToSalesTTM"] == pytest.approx(10.817, rel=1e-3)
    assert fmp_extra.fetch_scores("AAPL", get=fake_get)["altman_z"] == pytest.approx(13.5056, rel=1e-3) and fmp_extra.fetch_scores("AAPL", get=fake_get)["piotroski"] == 9
    r = fmp_extra.fetch_ratings("AAPL", get=fake_get)
    assert r == {"date": "2026-10-01", "strong_buy": 6, "buy": 19, "hold": 13, "sell": 3, "strong_sell": 3}
    assert fmp_extra.fetch_treasury_10y(get=fake_get) == (date(2026, 10, 16), 5.09)


@pytest.mark.db
def test_holders_skip_incomplete_quarter(db_conn):
    asked = []

    def get(path, **params):
        asked.append((params["year"], params["quarter"]))
        rows = fx("holders")
        return [{**r, "date": f"{params['year']}-{'03-31' if params['quarter'] == 1 else '06-30' if params['quarter'] == 2 else '09-30' if params['quarter'] == 3 else '12-31'}"} for r in rows]
    assert fmp_extra.ingest_holders(db_conn, 320193, "AAPL", get=get, today=date(2026, 10, 19)) == 10
    assert asked == [(2026, 2)]  # Q3 2026 ended 19 days ago: 13Fs are not due yet, so it is not asked for
    assert fmp_extra.load_holders(db_conn, 320193)[0]["quarter_end"] == date(2026, 6, 30)
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM holders")
    db_conn.commit()
    assert fmp_extra.ingest_holders(db_conn, 320193, "AAPL", get=get, today=date(2026, 12, 15), max_age_hours=0) == 10 and asked[-1] == (2026, 3)


@pytest.mark.db
def test_empty_result_counts_as_fresh(db_conn):
    calls = []

    def get(path, **params):
        calls.append(path)
        return []
    assert fmp_extra.ingest_dividends(db_conn, 1, "NOPAY", get=get) == 0
    assert fmp_extra.ingest_dividends(db_conn, 1, "NOPAY", get=get) == 0 and calls.count("dividends") == 1  # an empty answer is remembered for the refresh window
    assert fmp_extra.ingest_profile(db_conn, 1, "NOPAY", get=get) is False and fmp_extra.ingest_profile(db_conn, 1, "NOPAY", get=get) is False and calls.count("profile") == 1
    assert fmp_extra.ingest_peers(db_conn, 1, "NOPAY", get=get) == 0 and fmp_extra.ingest_peers(db_conn, 1, "NOPAY", get=get) == 0 and calls.count("stock-peers") == 1


def statements_get(path, **params):
    key = {"income-statement": "income", "balance-sheet-statement": "balance", "cash-flow-statement": "cashflow"}[path]
    return fx(f"{key}_{'annual' if params['period'] == 'annual' else 'quarter'}")


@pytest.mark.db
def test_statements_roundtrip(db_conn):
    n = fmp_extra.ingest_statements(db_conn, 320193, "AAPL", get=statements_get)
    st = fmp_extra.load_statements(db_conn, 320193)
    assert set(st) == {"income_annual", "income_quarter", "balance_annual", "balance_quarter", "cashflow_annual", "cashflow_quarter"} and n == sum(len(v) for v in st.values())
    inc = st["income_annual"]
    assert len(inc) == 8 and inc[0]["fiscal_year"] == 2025 and inc[0]["period"] == "FY" and inc[0]["revenue"] == 416161000000 and inc[0]["date"] == date(2025, 9, 27) and inc[0]["filing_date"] == date(2025, 10, 31)
    assert st["income_quarter"][0]["period"] == "Q3" and st["income_quarter"][0]["fiscal_year"] == 2026 and inc[0]["fiscal_year"] > inc[1]["fiscal_year"]
    assert fmp_extra.ingest_statements(db_conn, 320193, "AAPL", get=statements_get) == 0  # fresh
    assert fmp_extra.ingest_statements(db_conn, 320193, "AAPL", get=statements_get, force=True) == n


@pytest.mark.db
def test_statements_partial_failure(db_conn):
    def get(path, **params):
        if path == "cash-flow-statement":
            raise fmp_extra.fmp.FMPError("cash-flow-statement: 500")
        return statements_get(path, **params)
    assert fmp_extra.ingest_statements(db_conn, 320193, "AAPL", get=get) > 0
    st = fmp_extra.load_statements(db_conn, 320193)
    assert st["income_annual"] and st["balance_quarter"] and st["cashflow_annual"] == [] and st["cashflow_quarter"] == []

    def all_fail(path, **params):
        raise fmp_extra.fmp.FMPError("down")
    with pytest.raises(fmp_extra.fmp.FMPError):
        fmp_extra.fetch_statements("AAPL", get=all_fail)
