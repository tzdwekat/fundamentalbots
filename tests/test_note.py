"""The analyst note: sentences from templates over stored values, footnotes by first citation, sections in the framework's order."""
import pytest

from fundabot.analytics.metrics import Metric
from fundabot.analytics.red_flags import RedFlag
from fundabot.report import labels, note
from fundabot.report.table import GROUPS
from fundabot.retrieval.chunk import Chunk
from tests.test_context_pruning import aapl_metrics  # noqa: F401


def claim(cid, text, direction, evidence, strength=4.0, mat=3.0, path="screen", refutations=(), agent="bull/anthropic", weight=4):
    return {"id": cid, "agent": agent, "text": text, "direction": direction, "evidence": list(evidence), "weight": weight, "conceded_by": [],
            "refutations": [{"by": r, "reason": "no, because", "evidence": ["metric:net_debt@FY2025"]} for r in refutations], "withdrawn": False,
            "judge": {"evidence_strength": strength, "materiality": mat, "spread": 0 if not refutations else 2, "notes": ["judge note"], "path": path}}


def verdict():
    return {"ticker": "AAPL", "snapshot_id": "sidA", "composite_score": 1.9, "composite_uncapped": 1.9, "weighting": "evidence-weighted", "veto_note": None,
            "dispersion": 0.45, "dispersion_by_persona": {}, "dispersion_by_provider": {}, "quality_score": 84, "value_score": 12, "earnings_momentum_score": 76, "accounting_risk_score": 16,
            "consensus_claims": [claim("bull/anthropic#0", "Revenue growth re-accelerated to 16% in the latest quarter.", "positive", ["metric:revenue_yoy@Q3FY2026"]),
                                 claim("bear/openai#1", "Valuation sits at the top of its five-year range.", "negative", ["metric:pe_5y_percentile@current"], agent="bear/openai")],
            "contested_claims": [claim("skeptic/gemini#2", "Capital returns are unsustainable.", "negative", ["chunk:0000320193-25-000079#item7-3"], refutations=["bull/anthropic"], agent="skeptic/gemini", path="escalated")],
            "red_flags": [{"flag_id": "accruals_high", "severity": "high", "evidence": ["metric:accruals_ratio@FY2025"], "detail": "accruals ratio 12.0% in FY2025 (threshold 10%)"}],
            "catalysts": [{"what": "new product cycle", "when": "FY2027", "evidence": ["chunk:0000320193-25-000079#item7-3"]}],
            "one_paragraph_summary": "Strong operator, rich price.", "median_judge": "anthropic", "summaries": {"anthropic": "Strong operator, rich price.", "openai": "Fine but pricey."},
            "judge_agreement": 0.998, "implied_composites": {"anthropic": 1.9, "openai": 1.9}, "grid": {"bull/anthropic": {"score": 2, "confidence": 0.55, "hallucination_rate": 0.0, "model": "m"}},
            "agent_weights": {"bull/anthropic": 1.2}, "revised_scores": {"bull/anthropic": 2}, "providers": ["anthropic", "openai"],
            "judges": [{"provider": "anthropic", "model": "claude-opus-5-5"}], "screen_judges": [{"provider": "anthropic", "model": "claude-haiku-5-5"}],
            "escalated_ids": ["skeptic/gemini#2"], "n_claims": 3, "fallback_ids": [], "summary_unsupported": {}}


def analyses():
    return [{"persona": "bull", "provider": "anthropic", "model": "m", "agent": "bull/anthropic", "chunk_ids": ["0000320193-25-000079#item7-3"],
             "grounded": {"thesis": "Bull thesis.", "score": 5, "confidence": 0.6, "claims": [{"text": "Margins expanded.", "evidence": ["metric:gross_margin@FY2025"], "direction": "positive", "weight": 4, "unsupported_numbers": []}],
                          "dropped_claims": [], "key_risks": ["tariffs"], "what_would_change_my_mind": ["a margin reversal"], "stats": {"claims_total": 1, "claims_dropped": 0, "hallucination_rate": 0.0, "numbers_unsupported": 0, "numbers_total": 0},
                          "thesis_unsupported_numbers": [], "risks_unsupported_numbers": []}, "rebuttal": None},
            {"persona": "bear", "provider": "anthropic", "model": "m", "agent": "bear/anthropic", "chunk_ids": [],
             "grounded": {"thesis": "Bear thesis.", "score": -3, "confidence": 0.5, "claims": [{"text": "Debt is rising.", "evidence": ["metric:net_debt@FY2025"], "direction": "negative", "weight": 3, "unsupported_numbers": []}],
                          "dropped_claims": [], "key_risks": [], "what_would_change_my_mind": [], "stats": {"claims_total": 1, "claims_dropped": 0, "hallucination_rate": 0.0, "numbers_unsupported": 0, "numbers_total": 0},
                          "thesis_unsupported_numbers": [], "risks_unsupported_numbers": []}, "rebuttal": None}]


CHUNKS = {"0000320193-25-000079#item7-3": Chunk("0000320193-25-000079#item7-3", "0000320193-25-000079", "item7", 3, "We returned $90 billion to shareholders.", 12)}
FILINGS = [{"accession": "0000320193-25-000079", "form": "10-K", "filed": "2025-10-31", "primary_doc_url": "https://www.sec.gov/x/aapl-10k.htm"}]
SNAP = {"id": "sidA", "ticker": "AAPL", "price_date": "2026-07-31", "latest_close": 200.0, "accessions": ["0000320193-25-000079"]}
EXTRAS = {"profile": {"name": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics", "employees": 166000, "description": "Apple designs smartphones. " * 20},
          "segments": [{"kind": "product", "name": "iPhone", "fy": "FY2025", "share": 0.503, "yoy": 0.042, "revenue": 209.6e9, "prev_share": 0.51}], "peers": [{"peer": "SONY", "pe_ttm": 18.5}],
          "dividends": {}, "holders": [], "insider_trades": [], "scores": {"altman_z": 13.5, "piotroski": 9}, "ratings": {"date": "2026-10-01", "strong_buy": 6, "buy": 19, "hold": 13, "sell": 3, "strong_sell": 3},
          "next_earnings": "2026-11-02", "crosscheck": None, "edgar_crosscheck": None, "narrative": {"one_line": "Up on product news."}, "risk_free": None}


def test_labels_cover_groups():
    assert labels.unlabelled([i for g in GROUPS.values() for i in g]) == []
    assert labels.label("made_up_id") == "made up id"
    assert labels.seg_label("seg_product_iphone_share", {"name": "iPhone"}) == "iPhone share of revenue"


def test_fmt_kinds():
    assert labels.fmt(416.161e9, "usd") == "$416.2B" and labels.fmt(5.5e6, "usd") == "$5.5M" and labels.fmt(0.064, "pct") == "6.4%" and labels.fmt(39.38, "x") == "39.4x"
    assert labels.fmt(14776e6, "shares") == "14,776M shares" and labels.fmt(0, "shares") == "0 shares" and labels.fmt(7.46, "usd_share") == "$7.46" and labels.fmt(None, "pct") == "n/a"
    assert labels.flag_words(["derived", "source_conflict"]) == "derived; FMP and the filing disagree"


def test_fact_sentence_and_tone(aapl_metrics):
    lk = note.lookup(aapl_metrics)
    f = note.fact(lk, "revenue_yoy", "FY2025", "Revenue grew {v} in {p}", tone="growth")
    assert f["text"].startswith("Revenue grew ") and f["text"].endswith(" in FY2025") and "%" in f["text"] and f["ids"] == ["metric:revenue_yoy@FY2025"] and f["tone"] == "good"
    assert note.fact(lk, "net_debt", "FY2025", "Net debt {v} in {p}", tone="debt")["tone"] in ("good", "bad", "neutral")


def test_null_metrics_make_no_fact():
    lk = note.lookup([Metric("revenue_yoy", "FY2025", None, "ratio", "x", {}, [], None, "tag_missing")])
    assert note.fact(lk, "revenue_yoy", "FY2025", "Revenue grew {v} in {p}") is None and note.fact(lk, "missing", "FY2025", "{v}") is None


def test_footnotes_number_by_first_citation():
    fn = note.Footnotes()
    assert fn.ref(["metric:a@FY2025", "chunk:b"]) == "[1,2]" and fn.ref(["chunk:b"]) == "[2]" and fn.ref([]) == ""
    entries = fn.entries([Metric("a", "FY2025", 1.0, "USD", "xbrl:A", {"accession": "0000320193-25-000079"})], CHUNKS | {"b": Chunk("b", "0000320193-25-000079", "item1", 0, "text b", 2)}, FILINGS)
    assert [e["n"] for e in entries] == [1, 2] and "a@FY2025" in entries[0]["text"] and entries[1]["text"] == "text b" and entries[0]["url"] == "https://www.sec.gov/x/aapl-10k.htm"


VAL = [Metric("pe", "current", 39.4, "ratio", "x"), Metric("pe_5y_min", "current", 25.5, "ratio", "x"), Metric("pe_5y_median", "current", 30.0, "ratio", "x"), Metric("pe_5y_max", "current", 37.5, "ratio", "x"),
       Metric("pe_5y_percentile", "current", 1.0, "ratio", "x"), Metric("pe_peer_median", "current", 18.3, "ratio", "peers:median", {}, ["vendor_fmp"]), Metric("pe_vs_peer_median", "current", 2.15, "ratio", "peers:median", {}, ["vendor_fmp"]),
       Metric("dcf_bear", "current", 150.0, "USD/share", "dcf"), Metric("dcf_base", "current", 190.0, "USD/share", "dcf"), Metric("dcf_bull", "current", 240.0, "USD/share", "dcf")]


def with_valuation(ms):
    have = {(m.metric_id, m.period) for m in VAL}
    return [m for m in ms if (m.metric_id, m.period) not in have] + VAL


def test_build_note_sections_full(aapl_metrics):
    n = note.build_note("AAPL", SNAP, with_valuation(aapl_metrics), [RedFlag("accruals_high", "high", ["metric:accruals_ratio@FY2025"], "accruals ratio 12.0% in FY2025")], verdict(), analyses(), EXTRAS, CHUNKS, FILINGS)
    assert n["headline"]["company"] == "Apple Inc. · Technology · Consumer Electronics" and "mildly positive" in n["headline"]["reading"] and "judges agreed" in n["headline"]["reading"]
    assert len(n["headline"]["anchors"]) == 3
    assert any("iPhone" in f["text"] for f in n["business"]["facts"]) and any("166,000" in f["text"] for f in n["business"]["facts"]) and any("2026-11-02" in f["text"] for f in n["business"]["facts"])
    assert {"growth", "margins", "returns", "position", "cash_quality", "capital_allocation"} <= set(n["finances"]["tables"])
    assert any("Revenue" in r["label"] for r in n["finances"]["tables"]["growth"])
    assert n["red_flags"]["facts"][0]["text"].startswith("Accruals") and any("Valuation sits at the top" in c["text"] for c in n["red_flags"]["bears"])
    assert any("guidance" in f["text"].lower() or "estimate" in f["text"].lower() or "catalyst" in f["text"].lower() for f in n["strategy"]["facts"]) and any("new product cycle" in f["text"] for f in n["strategy"]["facts"])
    pe = next(f for f in n["valuation"]["facts"] if f["text"].startswith("P/E 39.4x"))
    assert "5-year range 25.5x–37.5x" in pe["text"] and "100th percentile" in pe["text"] and "2.15x the peer median of 18.3x" in pe["text"] and "metric:pe_5y_percentile@current" in pe["ids"]
    assert any(f["text"].startswith("DCF value $150.00 (bear) to $240.00 (bull), base $190.00 against a price of $200.00") for f in n["valuation"]["facts"])
    assert [c["text"] for c in n["debate"]["bull"]] == ["Revenue growth re-accelerated to 16% in the latest quarter."] and n["debate"]["bear"][0]["text"].startswith("Valuation sits")
    assert n["debate"]["contested"][0]["text"].startswith("Capital returns") and n["debate"]["contested"][0]["refutations"][0]["by"] == "bull/anthropic"
    assert n["debate"]["summary"] == "Strong operator, rich price." and "quality 84" in n["debate"]["subscores_text"]
    rd = n["appendix"]["run_details"]
    assert "claude-opus-5-5" in rd and "1 of 3 claims escalated" in rd and "Fine but pricey." in rd
    assert n["appendix"]["personas"][0]["persona"] == "bull" and n["appendix"]["metrics_table"] and n["appendix"]["sources"][0]["n"] == 1
    body_ids = [f["ids"] for sec in ("business", "finances", "red_flags", "strategy", "valuation") for f in n[sec]["facts"]]
    assert all(isinstance(ids, list) for ids in body_ids) and "[" in n["debate"]["bull"][0]["ref"]


def test_build_note_cheap_run(aapl_metrics):
    n = note.build_note("AAPL", SNAP, aapl_metrics, [], None, analyses(), EXTRAS, CHUNKS, FILINGS, run_kind="cheap")
    assert "no judge verdict yet" in n["headline"]["reading"] and n["headline"]["composite"] == 1.0  # mean of 5 and -3
    assert n["debate"]["bull"][0]["text"] == "Margins expanded." and n["debate"]["bear"][0]["text"] == "Debt is rising." and n["debate"]["contested"] == [] and n["debate"]["summary"] is None


def test_note_without_extras(aapl_metrics):
    n = note.build_note("AAPL", SNAP, aapl_metrics, [], verdict(), analyses(), {}, CHUNKS, FILINGS)
    assert n["headline"]["company"] == "AAPL" and all("None" not in f["text"] for sec in ("business", "finances", "strategy", "valuation") for f in n[sec]["facts"])
    assert not any("iPhone" in f["text"] for f in n["business"]["facts"])


def test_cheap_note_renders_without_verdict(aapl_metrics, tmp_path):
    from fundabot.report.render import render_report

    md_path, html_path = render_report("AAPL", SNAP, with_valuation(aapl_metrics), [], analyses(), CHUNKS, FILINGS, tmp_path, extras=EXTRAS)
    md = md_path.read_text()
    assert md.startswith("# AAPL — Apple Inc.") and "no judge verdict yet" in md and "## Business" in md and "## The debate" in md and "Margins expanded." in md and "Debt is rising." in md
    assert "# Appendix" in md and html_path.exists() and "<details" in html_path.read_text()


def test_footnotes_resolve_to_sources(aapl_metrics, tmp_path):
    from fundabot.report.render import render_verdict

    md_path, html_path = render_verdict("AAPL", SNAP, verdict(), analyses(), [], with_valuation(aapl_metrics), [], CHUNKS, FILINGS, tmp_path, "full", extras=EXTRAS)
    md, html = md_path.read_text(), html_path.read_text()
    body = md.split("# Appendix")[0]
    assert "Capital returns are unsustainable." in body and "[" in body
    assert "We returned $90 billion to shareholders." in md.split("# Appendix")[1] and "https://www.sec.gov/x/aapl-10k.htm" in md
    assert 'id="src-1"' in html and 'href="#src-' in html


def test_note_without_extras(aapl_metrics, tmp_path):
    from fundabot.report.render import render_verdict

    md = render_verdict("AAPL", SNAP, verdict(), analyses(), [], with_valuation(aapl_metrics), [], CHUNKS, FILINGS, tmp_path, "full")[0].read_text()
    assert "None" not in md.split("# Appendix")[0] and md.startswith("# AAPL")


def test_note_body_length_and_no_ids_outside_appendix(aapl_metrics, tmp_path):
    from fundabot.report.render import render_verdict

    md = render_verdict("AAPL", SNAP, verdict(), analyses(), [], with_valuation(aapl_metrics), [], CHUNKS, FILINGS, tmp_path, "full", extras=EXTRAS)[0].read_text()
    body = md.split("# Appendix")[0]
    assert len(body.splitlines()) < 200
    assert "metric:" not in body and "chunk:" not in body and "claude-" not in body and "gpt-" not in body
    assert "judges agreed" in body and "quality 84" in body
