import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fundabot.agents import context
from fundabot.analytics import periods, valuation
from fundabot.analytics.metrics import Metric, compute_fundamental_metrics
from fundabot.analytics.red_flags import RedFlag
from fundabot.ingest.edgar_facts import normalize
from fundabot.report.table import GROUPS

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def aapl_metrics():
    facts = normalize(json.loads((FIX / "AAPL_companyfacts.json").read_text()))
    series = periods.build_series(facts, periods.load_tag_map())
    fund = compute_fundamental_metrics(series)
    px = pd.DataFrame({"date": [date(2026, 7, 31)], "close": [200.0]})
    val = valuation.compute_valuation(series, facts, px, pd.DataFrame(columns=["accession", "form", "filed", "period_end"]), date(2026, 7, 31), fund)
    return fund + val


def ids(ms):
    return {f"{m.metric_id}@{m.period}" for m in ms}


def test_select_metrics_allowlist(aapl_metrics):
    shown = ids(context.select_metrics(aapl_metrics))
    assert "ocf@Q3FY2026" not in shown and "ocf@FY2025" in shown  # raw XBRL items at fiscal years only
    assert "gross_margin@FY2025" in shown and "revenue_yoy@Q3FY2026" in shown and "revenue_yoy@Q1FY2025" not in shown  # 4-quarter window
    assert {"fcf_ttm@current", "pe_5y_max@current", "dcf_base@current", "revenue_cagr_3y@FY2025"} <= shown
    group_ids = {m for g in GROUPS.values() for m in g}
    present = {m.metric_id for m in aapl_metrics}
    assert all(f"{g}@FY2025" in shown for g in group_ids & present if any(m.metric_id == g and m.period == "FY2025" for m in aapl_metrics))
    before = [m for m in aapl_metrics if m.period in context.select_periods(aapl_metrics)]
    assert len(context.select_metrics(aapl_metrics)) < 0.7 * len(before)  # measured: 400 rows (~13k tokens) vs 603 (~25k)


def test_red_flag_evidence_always_shown():
    ms = [Metric("accruals_ratio", "FY2019", 0.2, "ratio", "x")] + [Metric("revenue", f"FY{y}", 1.0, "USD", "x") for y in range(2020, 2026)]  # FY2019 is outside the 5-year window
    flags = [RedFlag("accruals_high", "high", ["metric:accruals_ratio@FY2019"], "d")]
    assert "accruals_ratio@FY2019" in ids(context.select_metrics(ms, flags)) and "accruals_ratio@FY2019" not in ids(context.select_metrics(ms))


def test_cited_ids_fixture_resolves(aapl_metrics):
    """Every metric id the stored AAPL/NVDA agents cited must stay citable: shown when the snapshot has it."""
    fixture = json.loads((FIX / "cited_metric_ids.json").read_text())
    from fundabot.report.table import columns_for

    cols = set(columns_for(aapl_metrics))
    shown_ids = {m.metric_id for m in context.select_metrics(aapl_metrics)}
    have_at_columns = {m.metric_id for m in aapl_metrics if m.period in cols}
    missing = sorted(m for m in fixture["metric_ids"] if m in have_at_columns and m not in shown_ids)
    assert missing == [], missing


def test_dedupe_boilerplate_keeps_newest():
    from fundabot.retrieval.chunk import Chunk
    from fundabot.retrieval.query import dedupe_boilerplate

    text = "Risks related to our business. " * 20
    old = Chunk("old#p2item1a-0", "old", "p2item1a", 0, text + "older", 50)
    new = Chunk("new#p2item1a-0", "new", "p2item1a", 0, text + "newer", 50)
    other = Chunk("new#item7-0", "new", "item7", 0, "Completely different text about results.", 8)
    kept = dedupe_boilerplate([old, new, other], {"new": 0, "old": 1})
    assert [c.id for c in kept] == ["new#p2item1a-0", "new#item7-0"]


@pytest.mark.db
def test_retrieve_excludes_ids(db_conn):
    from fundabot.retrieval.embed import FakeEmbedder
    from fundabot.retrieval.query import embed_missing, retrieve

    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO filings (accession, cik, form, filed, items_ok) VALUES ('acc-1', 1, '10-K', '2025-01-01', true)")
        cur.execute("INSERT INTO filing_sections (accession, item, text) VALUES ('acc-1', 'item1a', %s)", (" ".join(f"risk word{j}" for j in range(3000)),))
    db_conn.commit()
    embed_missing(db_conn, FakeEmbedder(), ["acc-1"])
    all_ids = {c.id for c in retrieve(db_conn, FakeEmbedder(), ["acc-1"], ["risk"], top_k=10)}
    first = next(iter(sorted(all_ids)))
    got = {c.id for c in retrieve(db_conn, FakeEmbedder(), ["acc-1"], ["risk"], top_k=10, exclude_ids={first})}
    assert first not in got and got <= all_ids and len(got) >= 1


def test_ground_uses_shown_values():
    from fundabot.eval.grounding import ground

    ms = [Metric("ocf", f"Q{q}FY{y}", v, "USD", "x") for (q, y, v) in ((1, 2024, 11.0e9), (2, 2024, 12.0e9), (3, 2024, 13.0e9), (4, 2024, 14.0e9), (1, 2025, 15.0e9), (2, 2025, 16.0e9), (3, 2025, 17.0e9), (4, 2025, 18.0e9))]
    ms.append(Metric("revenue", "FY2025", 100e9, "USD", "x"))
    out = {"thesis": "t", "claims": [{"text": "OCF was $11.0 billion", "evidence": ["metric:revenue@FY2025"], "direction": "positive", "weight": 1}],
           "key_risks": [], "what_would_change_my_mind": [], "score": 1, "confidence": 0.5}
    g = ground(out, ms, [])
    assert g["claims"][0]["unsupported_numbers"] == ["$11.0 billion"]  # Q1FY2024 is outside the shown 4-quarter window
