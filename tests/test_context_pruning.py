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
    assert "ocf@Q3FY2026" in shown and "ocf@Q2FY2026" not in shown and "ocf@FY2025" in shown  # raw XBRL items: fiscal years + newest quarter
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
    """Every metric:id@period the stored AAPL/NVDA agents cited must stay shown when the snapshot has that exact cell."""
    fixture = json.loads((FIX / "cited_metric_ids.json").read_text())
    shown = ids(context.select_metrics(aapl_metrics))
    have = ids(aapl_metrics)
    hidden = sorted(e[len("metric:"):] for e in fixture["evidence_ids"] if e.startswith("metric:") and e[len("metric:"):] in have and e[len("metric:"):] not in shown)
    qs = sorted({m.period for m in aapl_metrics if m.period.startswith("Q")}, key=lambda p: (int(p[4:]), int(p[1])))
    older_quarters = set(qs[:-1])  # the only cells pruning may hide: quarters before the newest one (the spec's 4-quarter window for groups, newest only for the rest)
    assert all(h.split("@")[1] in older_quarters for h in hidden), [h for h in hidden if h.split("@")[1] not in older_quarters]


def test_raw_and_derived_ids_shown_at_newest_quarter():
    """Between annual reports an agent reasons from the latest quarter's OCF, SBC, debt and equity, so raw XBRL and
    other derived ids are shown at the newest quarter column as well as at fiscal years."""
    ms = [Metric("revenue", f"FY{y}", 1.0, "USD", "x") for y in range(2021, 2026)]
    ms += [Metric("ocf", f"Q{q}FY2026", 1.0, "USD", "xbrl:ocf") for q in (1, 2, 3)]
    ms += [Metric("ebitda", f"Q{q}FY2026", 1.0, "USD", "ebitda") for q in (1, 2, 3)]
    shown = ids(context.select_metrics(ms))
    assert "ocf@Q3FY2026" in shown and "ebitda@Q3FY2026" in shown
    assert "ocf@Q1FY2026" not in shown and "ebitda@Q1FY2026" not in shown


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


# ---- Task 2: prefix / tail / user layout
from fundabot.agents import personas
from fundabot.retrieval.chunk import Chunk


def snap():
    return {"id": "s1", "ticker": "AAPL", "price_date": date(2026, 10, 14), "latest_close": 340.0, "accessions": ["acc", "T2026Q3"]}


def test_prefix_identical_across_personas(aapl_metrics):
    ps = personas.load_all()
    shared = [Chunk("acc#item1a-0", "acc", "item1a", 0, "Shared risk text.", 4)]
    own_bull = [Chunk("acc#item7-1", "acc", "item7", 1, "Bull-only growth text.", 4)]
    own_bear = [Chunk("acc#item1a-9", "acc", "item1a", 9, "Bear-only risk text.", 4)]
    p1, t1, u1 = context.build_messages("AAPL", snap(), aapl_metrics, [], shared, own_bull, ps["bull"], [])
    p2, t2, u2 = context.build_messages("AAPL", snap(), aapl_metrics, [], shared, own_bear, ps["bear"], ["note"])
    assert p1 == p2 and "METRICS" in p1 and "SHARED_EXCERPTS" in p1 and "Shared risk text." in p1 and "Cite a metric id or chunk id" in p1
    assert t1 != t2 and "bull" in t1 and "bear" in t2 and "Role and bias" in t1
    assert "Bull-only growth text." in u1 and "Bull-only" not in p1 and "PERSONA_EXCERPTS" in u1 and "note" in u2 and u1.rstrip().endswith("Write your analysis as JSON following the schema.")
    assert context.system_prompt(ps["bull"]) and context.build_user_message("AAPL", snap(), aapl_metrics, [], shared, [])  # old entry points still work


def test_persona_with_no_unique_chunks(aapl_metrics):
    p, t, u = context.build_messages("AAPL", snap(), aapl_metrics, [], [Chunk("acc#item1a-0", "acc", "item1a", 0, "Shared.", 2)], [], personas.load_all()["bull"], [])
    assert "no additional excerpts" in u and u.rstrip().endswith("Write your analysis as JSON following the schema.")


def test_unshown_metric_still_resolves_as_evidence():
    from fundabot.eval.grounding import ground

    ms = [Metric("ocf", "Q1FY2024", 11.0e9, "USD", "x")] + [Metric("revenue", f"FY{y}", 1.0, "USD", "x") for y in range(2021, 2026)]
    out = {"thesis": "t", "claims": [{"text": "cash flow was fine", "evidence": ["metric:ocf@Q1FY2024"], "direction": "positive", "weight": 1}],
           "key_risks": [], "what_would_change_my_mind": [], "score": 1, "confidence": 0.5}
    g = ground(out, ms, [])
    assert g["claims"] and g["claims"][0]["evidence_resolved"] is True and g["dropped_claims"] == []


@pytest.mark.db
async def test_grid_stores_shared_plus_own_chunk_ids(db_conn):
    from fundabot.agents.adapters.fake import FakeAdapter
    from fundabot.agents.runner import run_grid
    from fundabot.orchestrate import snapshot
    from fundabot.retrieval.embed import FakeEmbedder
    from fundabot.retrieval.query import embed_missing

    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["acc-1"], date(2026, 10, 14), 300.0, 1e10)
    snapshot.store_metrics(db_conn, sid, [Metric("revenue", "FY2024", 1.0, "USD", "x")])
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO filings (accession, cik, form, filed, items_ok) VALUES ('acc-1', 320193, '10-K', '2025-01-01', true)")
        for item in ("item1a", "item7", "item1"):
            cur.execute("INSERT INTO filing_sections (accession, item, text) VALUES ('acc-1', %s, %s)", (item, " ".join(f"{item} word{j}" for j in range(2500))))
    db_conn.commit()
    embed_missing(db_conn, FakeEmbedder(), ["acc-1"])
    out = {"thesis": "t", "claims": [], "key_risks": [], "what_would_change_my_mind": [], "score": 1, "confidence": 0.5}
    a = FakeAdapter([out]); a.provider, a.model = "anthropic", "m"
    rows = await run_grid(db_conn, sid, ["bull", "bear"], [a], FakeEmbedder())
    assert a.last_prefix and "METRICS" in a.last_prefix
    ids = [set(r["chunk_ids"]) for r in rows]
    assert ids[0] & ids[1]  # shared chunks appear in both agents' stored ids
    assert all(len(set(r["chunk_ids"])) == len(r["chunk_ids"]) for r in rows)  # no duplicates


def test_snapshot_block_has_company_line(aapl_metrics):
    snap = {"id": "s", "price_date": None, "latest_close": None, "accessions": [], "profile": {"name": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics", "employees": 166000, "description": "Apple Inc. designs " + "x" * 300}}
    text = context.shared_prefix("AAPL", snap, aapl_metrics, [], [])
    head = text.split("METRICS")[0]
    assert "company: Apple Inc. · Technology · Consumer Electronics · 166,000 employees" in head and "about: Apple Inc. designs" in head and len(head) < 900
    assert "company:" not in context.shared_prefix("AAPL", {**snap, "profile": None}, aapl_metrics, [], []).split("METRICS")[0]


def test_select_metrics_shows_fmp_base_items():
    ms = [Metric("revenue", f"FY{y}", 1.0, "USD", "fmp:income:revenue") for y in range(2021, 2026)] + [Metric("revenue", f"Q{q}FY2026", 1.0, "USD", "fmp:income:revenue") for q in (1, 2, 3)]
    shown = ids(context.select_metrics(ms))
    assert "revenue@FY2025" in shown and "revenue@Q3FY2026" in shown
    ms2 = [Metric("sbc", f"FY{y}", 1.0, "USD", "fmp:cashflow:stockBasedCompensation") for y in range(2021, 2026)] + [Metric("sbc", f"Q{q}FY2026", 1.0, "USD", "fmp:cashflow:stockBasedCompensation") for q in (1, 2, 3)] + ms
    shown2 = ids(context.select_metrics(ms2))
    assert "sbc@FY2025" in shown2 and "sbc@Q3FY2026" in shown2 and "sbc@Q1FY2026" not in shown2  # raw items: fiscal years + newest quarter, as for xbrl
