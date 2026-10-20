"""Per-ticker pipeline. prepare() = ingest -> snapshot -> metrics -> flags. Agent stages are added later."""
import logging
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import httpx
import psycopg
from psycopg.types.json import Jsonb

from fundabot.analytics import earnings, periods, red_flags, valuation
from fundabot.analytics.metrics import compute_fundamental_metrics
from fundabot.ingest import edgar_facts, edgar_filings, fmp, prices, transcripts
from fundabot.orchestrate import snapshot

log = logging.getLogger("fundabot")


def _guidance_label(fye, offset):
    from fundabot.orchestrate.triggers import transcript_key_for

    def label(row):
        if row.get("call_date"):
            fy, fq = (int(x) for x in row["key"][1:].split("Q"))
            return transcript_key_for(row["call_date"], fy, fq, fye, offset)
        return f"Q{row['key'].split('Q')[1]}FY{row['key'][1:].split('Q')[0]}"
    return label


@contextmanager
def _stage(ticker: str, name: str):
    """INFO-level wall time per pipeline stage, so a slow run can be attributed from the log."""
    t0 = time.monotonic()
    yield
    log.info("%s: %s took %.0fs", ticker, name, time.monotonic() - t0)


async def ensure_guidance(conn: psycopg.Connection, sid: str, adapter) -> int:
    """Extract guidance for the snapshot's transcripts that have none yet, then refresh the snapshot's guidance metrics. Returns extractions made."""
    from fundabot.analytics import guidance as guidance_mod
    from fundabot.retrieval.chunk import chunk_section

    s = snapshot.load(conn, sid)
    keys = [a for a in s["accessions"] if a.startswith("T")]
    if not keys:
        return 0
    have = {r["key"] for r in guidance_mod.load(conn, s["cik"], keys)}
    made = 0
    for k in keys:
        if k in have:
            continue
        sections = edgar_filings.load_sections(conn, k)
        if "call" in sections:
            try:
                with _stage(s["ticker"], f"guidance {k}"):
                    await guidance_mod.extract(conn, s["cik"], k, adapter, chunk_section(k, "call", sections["call"]))
                made += 1
            except Exception as e:  # guidance is optional input; a failed extraction must not stop the analysis
                log.warning("guidance extraction failed for %s %s: %s", s["ticker"], k, e)
                conn.rollback()
    if made:
        from fundabot.orchestrate.triggers import fiscal_calendar

        try:
            label_fn = _guidance_label(*fiscal_calendar(conn, s["cik"]))
        except periods.InsufficientHistory:  # no XBRL facts yet: fall back to FMP's own fiscal labels
            label_fn = None
        rows = guidance_mod.metrics(guidance_mod.load(conn, s["cik"], keys), label_fn=label_fn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM metrics WHERE snapshot_id = %s AND metric_id IN ('guidance_direction', 'management_tone')", (sid,))
        conn.commit()
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO metrics (snapshot_id, metric_id, period, value, unit, formula_id, inputs, flags, tag_used, null_reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            [(sid, m.metric_id, m.period, m.value, m.unit, m.formula_id, snapshot.jsonb(m.inputs), snapshot.jsonb(m.flags), m.tag_used, m.null_reason) for m in rows])
        conn.commit()
    return made


def _recent_fetch(conn: psycopg.Connection, cik: int, max_age_hours: float = 24) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM raw_companyfacts WHERE cik = %s AND fetched_at > now() - make_interval(hours => %s) ORDER BY id DESC LIMIT 1",
                    (cik, max_age_hours))
        row = cur.fetchone()
    return row[0] if row else None


def _treasury_json(t):
    return {"date": t[0].isoformat(), "year10": t[1]} if t else None


def _treasury(v):
    return (date.fromisoformat(v["date"]), float(v["year10"])) if v else None


def prepare(conn: psycopg.Connection, ticker: str, force: bool = False) -> str:
    """Ingest -> snapshot -> metrics -> flags (no model calls; guidance metrics come from cached extractions)."""
    ticker = ticker.upper()
    cik = edgar_facts.ticker_to_cik(ticker)
    fetch_id = None if force else _recent_fetch(conn, cik)
    if fetch_id is None:
        _, fetch_id = edgar_facts.ingest(conn, ticker)
    facts = edgar_facts.load_facts(conn, cik)

    try:
        price_date = prices.ingest(conn, cik, ticker)
        prices_df = prices.load(conn, cik)
        prices.ingest_splits(conn, cik, ticker)
    except prices.PriceUnavailable as e:
        log.warning("prices unavailable for %s: %s", ticker, e)
        price_date, prices_df = None, None
    as_of = price_date or date.today()
    facts = periods.adjust_for_splits(facts, prices.load_splits(conn, cik))

    filings = edgar_filings.list_filings(cik)
    accessions = edgar_filings.select_for_snapshot(filings)
    edgar_filings.ingest(conn, cik, filings, accessions, filings.attrs.get("name", ""))
    try:
        accessions = accessions + transcripts.ingest(conn, cik, ticker)
    except (fmp.FMPError, httpx.HTTPError, ValueError) as e:  # transcripts are optional input
        log.warning("transcripts unavailable for %s: %s", ticker, e)

    tag_map = periods.load_tag_map()
    series = periods.build_series(facts, tag_map)
    fundamentals = compute_fundamental_metrics(series)
    val = valuation.compute_valuation(series, facts, prices_df, filings, as_of, fundamentals)
    try:
        fmp.ingest(conn, cik, ticker)
    except (fmp.FMPError, httpx.HTTPError, ValueError) as e:  # vendor outage must not fail the run; metrics degrade to no_coverage
        log.warning("FMP unavailable for %s: %s", ticker, e)
    fmp_data = fmp.load(conn, cik)
    latest_fetch = fmp_data["estimates"].fetched_at.max() if len(fmp_data["estimates"]) else None
    cur_price = next((m.value for m in val if m.metric_id == "price" and m.period == "current"), None)
    earn = earnings.compute_earnings_metrics(fmp_data, as_of, cur_price, periods.fiscal_year_end(facts, tag_map), periods.fy_offset(facts, tag_map), latest_fetch)
    from fundabot.analytics import fmp_metrics
    from fundabot.ingest import fmp_extra

    for step in (fmp_extra.ingest_profile, fmp_extra.ingest_peers, fmp_extra.ingest_segments, fmp_extra.ingest_dividends, fmp_extra.ingest_volume,
                 fmp_extra.ingest_insider_trades, fmp_extra.ingest_holders):
        try:
            step(conn, cik, ticker)
        except Exception as e:  # each dataset is optional input; stored rows stay
            log.warning("%s: %s failed: %s", ticker, step.__name__, e)
            conn.rollback()
    profile = fmp_extra.load_profile(conn, cik) or {}
    own = {m.metric_id: m.value for m in val if m.period == "current" and m.metric_id in ("pe", "ev_sales")}
    fmp_m = fmp_metrics.compute_fmp_metrics(fmp_extra.load_peers(conn, cik), fmp_extra.load_segments(conn, cik), fmp_extra.load_dividends(conn, cik),
                                            fmp_extra.fact(conn, cik, "scores", lambda: fmp_extra.fetch_scores(ticker), 24),
                                            _treasury(fmp_extra.fact(conn, 0, "treasury_10y", lambda: _treasury_json(fmp_extra.fetch_treasury_10y()), 24)), cur_price, own, today=as_of)
    from fundabot.analytics import guidance as guidance_mod

    fye, offset = periods.fiscal_year_end(facts, tag_map), periods.fy_offset(facts, tag_map)
    guid = guidance_mod.metrics(guidance_mod.load(conn, cik, [a for a in accessions if a.startswith("T")]), label_fn=_guidance_label(fye, offset))
    flags = red_flags.numeric_flags(fundamentals + fmp_m, profile.get("sector"), profile.get("industry"))
    latest_10k = next((a for a in accessions if filings[filings.accession == a].form.iloc[0] == "10-K"), None)
    if latest_10k:
        flags += red_flags.text_flags(latest_10k, edgar_filings.load_sections(conn, latest_10k), filings)

    cur = {m.metric_id: m for m in val if m.period == "current"}
    close = cur["price"].value if "price" in cur else None
    shares = cur["shares_outstanding"].value if "shares_outstanding" in cur else None
    sid = snapshot.create_snapshot(conn, ticker, cik, fetch_id, accessions, price_date, close, shares)
    snapshot.store_metrics(conn, sid, fundamentals + val + earn + guid + fmp_m)
    from fundabot.report.verify import crosscheck_fmp

    ttm = fmp_extra.fact(conn, cik, "ttm", lambda: fmp_extra.fetch_ttm(ticker), 24) or {}
    rows = crosscheck_fmp(fundamentals + val + fmp_m, ttm) if ttm else []
    fmp_extra.fact(conn, cik, "fmp_crosscheck", lambda: {"checked": as_of.isoformat(), "rows": rows, "ok": sum(1 for r in rows if r["ok"]), "total": sum(1 for r in rows if r["ok"] is not None)}, 0)
    snapshot.store_flags(conn, sid, flags)
    return sid


async def analyze(conn: psycopg.Connection, ticker: str, personas: list[str], adapter, embedder, force: bool = False, rerun: bool = False,
                  out_dir: Path = Path("reports"), guidance_adapter=None) -> Path:
    """Phase 2 run over one or more providers. `adapter` may be one Adapter or a list. Cached analyses are reused unless rerun=True."""
    from fundabot.agents.runner import run_grid
    from fundabot.report.render import render_report

    adapters = adapter if isinstance(adapter, list) else [adapter]
    from fundabot.retrieval.query import embed_missing, load_chunks

    with _stage(ticker, "prepare"):
        sid = prepare(conn, ticker, force=force)
    if guidance_adapter is not None:
        with _stage(ticker, "guidance"):
            await ensure_guidance(conn, sid, guidance_adapter)
    s = snapshot.load(conn, sid)
    with _stage(ticker, "embed"):
        embed_missing(conn, embedder, s["accessions"])
    if rerun:
        with conn.cursor() as cur:
            for a in adapters:
                cur.execute("DELETE FROM analyses WHERE snapshot_id = %s AND provider = %s AND model = %s", (sid, a.provider, a.model))
        conn.commit()
    analyses = await run_grid(conn, sid, personas, adapters, embedder)  # grounded inside run_grid
    metrics, flags = snapshot.load_metrics(conn, sid), snapshot.load_flags(conn, sid)
    chunks_by_id = load_chunks(conn, sorted({c for a in analyses for c in a["chunk_ids"]}))
    with conn.cursor() as cur:
        cur.execute("SELECT accession, form, filed FROM filings WHERE accession = ANY(%s) ORDER BY filed DESC", (s["accessions"],))
        filings = [dict(zip(["accession", "form", "filed"], r)) for r in cur.fetchall()]
    _, html = render_report(ticker, s, metrics, flags, analyses, chunks_by_id, filings, out_dir)
    log.info("%s: report %s", ticker, html)
    return html


async def analyze_full(conn: psycopg.Connection, ticker: str, personas: list[str], adapters: list, screen_judges: list, judges: list, embedder,
                       force: bool = False, rerun: bool = False, out_dir: Path = Path("reports"), guidance_adapter=None,
                       run_kind: str = "full", snapshot_id: str | None = None) -> Path:
    """Grid -> rebuttal -> screen panel (every claim) -> escalation panel (disputed / material claims, with evidence) -> scoring
    -> verdict report. Every round is cached per snapshot. snapshot_id: re-judge that stored snapshot (no prepare, no guidance);
    run_kind names the verdicts row and the report file."""
    from fundabot.agents.runner import run_grid
    from fundabot.debate import claims, judge, rebuttal, scoring
    from fundabot.eval import grounding
    from fundabot.report.render import render_verdict
    from fundabot.retrieval.query import embed_missing, load_chunks

    if snapshot_id is None:
        with _stage(ticker, "prepare"):
            sid = prepare(conn, ticker, force=force)
        if guidance_adapter is not None:
            with _stage(ticker, "guidance"):
                await ensure_guidance(conn, sid, guidance_adapter)
    else:
        sid = snapshot_id
    s = snapshot.load(conn, sid)
    with _stage(ticker, "embed"):
        embed_missing(conn, embedder, s["accessions"])
    if rerun:
        with conn.cursor() as cur:
            for table in ("analyses", "rebuttals", "judgements", "verdicts"):
                cur.execute(f"DELETE FROM {table} WHERE snapshot_id = %s", (sid,))
        conn.commit()

    from fundabot.agents.runner import load_analyses

    if snapshot_id is not None:  # re-judge only: the stored grid as it is, failed cells and all, so nothing invalidates the stored rounds
        analyses = load_analyses(conn, sid)
    else:
        before = {(a["persona"], a["provider"], a["model"]) for a in load_analyses(conn, sid)}
        with _stage(ticker, "grid"):
            analyses = await run_grid(conn, sid, personas, adapters, embedder)
        if {(a["persona"], a["provider"], a["model"]) for a in analyses} - before:
            # new cells joined the pool: cached rebuttals and judgements never saw their claims
            with conn.cursor() as cur:
                for table in ("rebuttals", "judgements", "verdicts"):
                    cur.execute(f"DELETE FROM {table} WHERE snapshot_id = %s", (sid,))
            conn.commit()
    metrics, flags = snapshot.load_metrics(conn, sid), snapshot.load_flags(conn, sid)
    chunks_by_id = load_chunks(conn, sorted({c for a in analyses for c in a["chunk_ids"]}))
    accessions = s["accessions"]

    def resolvable(eid: str) -> bool:
        return eid in claims.evidence_text([eid], metrics, chunks_by_id, accessions)

    pool = claims.build_pool(analyses)
    with _stage(ticker, "rebuttal"):
        rebuttals = await rebuttal.run_rebuttals(conn, sid, analyses, pool, {a.provider: a for a in adapters}, embedder, metrics, chunks_by_id, accessions)
    pool, revised = rebuttal.apply_rebuttals(pool, rebuttals, resolvable)
    for a in analyses:
        revised.setdefault(a["agent"], (a["grounded"]["score"], a["grounded"]["confidence"]))

    judged = judge.precheck(pool, resolvable)
    earnings_available = any(m.metric_id in earnings.IDS and m.value is not None for m in metrics)

    def evidence_for(claims_subset):
        ids = sorted({e for c in claims_subset for e in c["evidence"]} | {e for c in claims_subset for r in c["refutations"] for e in r["evidence"]})
        return claims.evidence_text(ids, metrics, chunks_by_id, accessions)

    with _stage(ticker, "judges"):
        round1 = await judge.run_judges(conn, sid, screen_judges, judge.build_message(judged, flags, metrics, earnings_available, evidence_for(judged), revised))
        judged_ids = [c["id"] for c in judged]
        if round1:
            escalated = judge.escalate(round1, judged, len(screen_judges))
        else:
            log.warning("%s: no screen judge succeeded; the strong panel judges every claim", ticker)
            escalated = sorted(judged_ids)
        screen_means = judge.mean_scores(round1, judged_ids)
        esc_pool = [c for c in judged if c["id"] in set(escalated)]
        unanimous = [(c, screen_means[c["id"]]) for c in judged if c["id"] not in set(escalated)]
        message2 = judge.build_escalation_message(esc_pool, round1, unanimous, flags, metrics, earnings_available, evidence_for(esc_pool), revised)
        strong = await judge.run_escalation(conn, sid, judges, {j.provider: message2 for j in judges}, escalated)
        if not strong:
            raise RuntimeError(f"{ticker}: no judge succeeded; no verdict produced")
    means = judge.merge_panels(screen_means, judge.mean_scores(strong, escalated), escalated)
    fallback_ids = sorted(k for k, v in means.items() if v.get("fallback"))
    if fallback_ids:
        log.warning("%s: %d claims escalated but unscored by every strong judge; the screen panel's scores stand: %s", ticker, len(fallback_ids), fallback_ids[:8])
    passthrough = {k: v for k, v in means.items() if v["path"] == "screen"}
    implied = [scoring.implied_from_means(judged, {**passthrough, **{c["claim_id"]: {"evidence_strength": float(c["evidence_strength"])} for c in j["output"]["claims"]}}, revised)
               for j in strong]
    reconciled = strong  # the strong panel supplies sub-scores, catalysts and summaries
    cells = {(a["persona"], a["provider"]): {"score": a["grounded"]["score"], "confidence": a["grounded"]["confidence"],
                                             "hallucination_rate": a["grounded"]["stats"]["hallucination_rate"], "model": a["model"]} for a in analyses}
    raw_catalysts = [{**c, "evidence": [e for e in c["evidence"] if resolvable(e)]} for j in reconciled for c in j["output"]["catalysts"]]
    catalysts = claims.dedupe([c for c in raw_catalysts if c["evidence"]], embedder, key="what")  # a catalyst with no real evidence is dropped
    verdict = scoring.build_verdict(ticker.upper(), sid, judged, revised, means, reconciled, implied, cells, catalysts, [f.to_dict() for f in flags])
    verdict["summary_unsupported"] = {j["provider"]: grounding.unsupported_numbers(j["output"]["one_paragraph_summary"], metrics, list(chunks_by_id.values()), flags)
                                      for j in reconciled}
    verdict["providers"] = [a.provider for a in adapters]
    verdict["judges"] = [{"provider": j["provider"], "model": j["model"]} for j in reconciled]
    verdict["screen_judges"] = [{"provider": j["provider"], "model": j["model"]} for j in round1]
    verdict["escalated_ids"], verdict["n_claims"], verdict["fallback_ids"] = escalated, len(judged), fallback_ids
    with conn.cursor() as cur:
        cur.execute("INSERT INTO verdicts (snapshot_id, run_kind, verdict) VALUES (%s, %s, %s) ON CONFLICT (snapshot_id, run_kind) DO UPDATE SET verdict = EXCLUDED.verdict, created_at = now()",
                    (sid, run_kind, Jsonb(verdict)))
        cur.execute("SELECT accession, form, filed FROM filings WHERE accession = ANY(%s) ORDER BY filed DESC", (accessions,))
        filings = [dict(zip(["accession", "form", "filed"], r)) for r in cur.fetchall()]
    conn.commit()
    _, html = render_verdict(ticker, s, verdict, analyses, rebuttals, metrics, flags, chunks_by_id, filings, out_dir, run_kind)
    log.info("%s: verdict %s composite %s", ticker, html, verdict["composite_score"])
    return html
