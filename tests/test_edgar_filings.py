from datetime import date

import pandas as pd
import pytest

from fundabot.ingest import edgar_filings


def filings_df(rows):
    return pd.DataFrame(rows, columns=["accession", "form", "filed", "period_end", "primary_doc_url"])


def test_select_for_snapshot_counts():
    rows = [(f"k{i}", "10-K", date(2019 + i, 2, 1), date(2018 + i, 12, 31), "") for i in range(7)]
    rows += [(f"q{i}", "10-Q", date(2023, 1, 1) + pd.Timedelta(days=90 * i).to_pytimedelta(), None, "") for i in range(12)]
    rows += [("ka", "10-K/A", date(2026, 3, 1), None, ""), ("e", "8-K", date(2026, 3, 2), None, "")]
    acc = edgar_filings.select_for_snapshot(filings_df(rows))
    assert acc[:5] == ["k6", "k5", "k4", "k3", "k2"]
    assert acc[5:] == [f"q{i}" for i in range(11, 3, -1)]


def test_foreign_filer_rejected():
    df = filings_df([("a", "20-F", date(2026, 3, 1), None, ""), ("b", "6-K", date(2026, 5, 1), None, "")])
    with pytest.raises(edgar_filings.UnsupportedFiler):
        edgar_filings.select_for_snapshot(df)


def test_parse_submissions():
    payload = {"cik": "320193", "filings": {"recent": {
        "accessionNumber": ["0000320193-25-000073", "0001140361-26-039158"],
        "form": ["10-K", "4"], "filingDate": ["2025-10-31", "2026-10-08"], "reportDate": ["2025-09-27", ""],
        "primaryDocument": ["aapl-20250927.htm", "xslF345X06/form4.xml"],
    }}}
    df = edgar_filings.parse_submissions(payload)
    assert list(df.columns) == ["accession", "form", "filed", "period_end", "primary_doc_url"]
    assert df.iloc[0].period_end == date(2025, 9, 27) and pd.isna(df.iloc[1].period_end)
    assert df.iloc[0].primary_doc_url == "https://www.sec.gov/Archives/edgar/data/320193/000032019325000073/aapl-20250927.htm"
