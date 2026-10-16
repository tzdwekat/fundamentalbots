"""Earnings call transcripts (FMP) stored as sections under the synthetic accession T{fy}Q{fq}, item 'call'.

FMP's fiscal year/quarter are the company's own convention and are used as-is for the key.
"""
import logging
from datetime import date

import psycopg

from fundabot.ingest import fmp

log = logging.getLogger("fundabot")


def key(fy: int, fq: int) -> str:
    return f"T{fy}Q{fq}"


def ingest(conn: psycopg.Connection, cik: int, ticker: str, n: int = 4) -> list[str]:
    """Ensure the newest n transcripts are stored; returns their keys newest first."""
    dates = sorted(fmp.fetch_transcript_dates(ticker), key=lambda d: (d["fiscalYear"], d["quarter"]), reverse=True)[:n]
    with conn.cursor() as cur:
        cur.execute("SELECT fy, fq FROM transcripts WHERE cik = %s", (cik,))
        have = set(cur.fetchall())
    keys = []
    for d in dates:
        fy, fq = int(d["fiscalYear"]), int(d["quarter"])
        k = key(fy, fq)
        keys.append(k)
        if (fy, fq) in have:
            continue
        t = fmp.fetch_transcript(ticker, fy, fq)
        if not t or not t.get("content"):
            log.warning("no transcript content for %s %s", ticker, k)
            keys.pop()
            continue
        call_date = date.fromisoformat(t["date"][:10]) if t.get("date") else date.fromisoformat(d["date"][:10])
        with conn.cursor() as cur:
            cur.execute("INSERT INTO transcripts (cik, fy, fq, call_date, content) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (cik, fy, fq, call_date, t["content"]))
            cur.execute("INSERT INTO filings (accession, cik, form, filed, period_end, primary_doc_url, items_ok) VALUES (%s,%s,'TRANSCRIPT',%s,%s,NULL,true) ON CONFLICT (accession) DO NOTHING",
                        (k, cik, call_date, call_date))
            cur.execute("INSERT INTO filing_sections (accession, item, text) VALUES (%s,'call',%s) ON CONFLICT DO NOTHING", (k, t["content"]))
        conn.commit()
    return keys
