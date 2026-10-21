"""The analyst note: sections in the analyst's order, each fact a sentence from a template over a stored value, with
footnotes to the source. Nothing here calls a model or a network; the only model text is quoted and attributed."""
import json

from fundabot.agents.context import metric_line
from fundabot.analytics.metrics import Metric
from fundabot.analytics.valuation import DISCOUNT_RATE, TERMINAL_GROWTH, YEARS
from fundabot.report import labels
from fundabot.report.table import GROUPS, columns_for, fmt as table_fmt

FLAG_LABELS = {"accruals_high": "Accruals are high", "fcf_below_ni": "Free cash flow trails net income", "goodwill_heavy": "Goodwill-heavy balance sheet",
               "going_concern": "Going-concern language in the filing", "late_filing": "Late filing", "altman_z_distress": "Altman Z in the distress zone"}
GOOD_UP = {"growth", "margin", "return", "yield", "coverage"}


def lookup(metrics: list[Metric]) -> dict:
    return {(m.metric_id, m.period): m for m in metrics}


def _tone(value: float, tone: str | None) -> str | None:
    """Only a change has a direction: growth rates colour by sign; levels (a margin, a multiple) carry no colour here."""
    if tone == "growth":
        return "good" if value > 0 else "bad" if value < 0 else "neutral"
    return None


def fact(lk: dict, mid: str, period: str, template: str, tone: str | None = None, extra_ids: list[str] | None = None) -> dict | None:
    m = lk.get((mid, period))
    if m is None or m.value is None:
        return None
    k = labels.kind(mid)
    text = template.format(v=labels.fmt(m.value, k), p=period, l=labels.label(mid))
    return {"text": text, "value": labels.fmt(m.value, k), "ids": [f"metric:{mid}@{period}"] + list(extra_ids or []), "tone": _tone(float(m.value), tone),
            "flags_text": labels.flag_words(m.flags)}


class Footnotes:
    def __init__(self):
        self.order: list[str] = []

    def ref(self, ids) -> str:
        nums = []
        for i in ids:
            if i not in self.order:
                self.order.append(i)
            nums.append(str(self.order.index(i) + 1))
        return f"[{','.join(nums)}]" if nums else ""

    def entries(self, metrics: list[Metric], chunks_by_id: dict | None, filings) -> list[dict]:
        urls = {f["accession"]: f.get("primary_doc_url") for f in (filings or []) if isinstance(f, dict)}
        by_key = {f"metric:{m.metric_id}@{m.period}": m for m in metrics}
        by_id = {}
        for m in metrics:
            by_id.setdefault(f"metric:{m.metric_id}", m)
        out = []
        for n, eid in enumerate(self.order, 1):
            text, url, inputs = eid, None, ""
            if eid.startswith("metric:"):
                m = by_key.get(eid) or by_id.get(eid)
                if m is not None:
                    text = metric_line(m)
                    inp = dict(m.inputs or {})
                    acc = inp.pop("accession", None) or next((v.get("accession") for v in inp.values() if isinstance(v, dict) and v.get("accession")), None)
                    url = urls.get(acc)
                    inputs = json.dumps({k: v for k, v in inp.items() if k not in ("start", "end")}, default=str)[:300] if inp else ""
            elif eid.startswith("chunk:"):
                c = (chunks_by_id or {}).get(eid.split(":", 1)[1])
                if c is not None:
                    text, url = c.text, urls.get(c.accession)
            out.append({"n": n, "id": eid, "text": text, "url": url, "inputs": inputs})
        return out


def reading(composite: float | None, agreement: float | None, cheap: bool = False) -> str:
    if composite is None:
        return "no score yet"
    band = "strongly positive" if composite >= 5 else "positive" if composite >= 2 else "mildly positive" if composite > 0 else "mildly negative" if composite > -2 else "negative" if composite > -5 else "strongly negative"
    head = f"{composite:+.1f} on a -10..10 scale: {band}"
    if cheap:
        return head + "; mean of the persona scores, no judge verdict yet"
    if agreement is None:
        return head
    return head + (f"; judges agreed ({agreement:.3f})" if agreement >= 0.9 else f"; judges disagreed (agreement {agreement:.3f})")


def _claim_view(c: dict, fn: Footnotes) -> dict:
    j = c.get("judge") or {}
    return {"id": c.get("id"), "text": c["text"], "agent": c.get("agent", ""), "direction": c["direction"], "ref": fn.ref(c.get("evidence", [])), "ids": c.get("evidence", []),
            "strength": j.get("evidence_strength"), "materiality": j.get("materiality"), "path": j.get("path"), "notes": j.get("notes", []),
            "refutations": [{"by": r["by"], "reason": r["reason"], "ref": fn.ref(r.get("evidence", []))} for r in c.get("refutations") or []], "conceded_by": c.get("conceded_by", []), "weight": c.get("weight")}


def _growth_rows(lk, fys, qs):
    rows = []
    for mid, yoy, c3, c5 in (("revenue", "revenue_yoy", "revenue_cagr_3y", "revenue_cagr_5y"), ("eps_diluted", "eps_diluted_yoy", "eps_diluted_cagr_3y", "eps_diluted_cagr_5y"), ("fcf", "fcf_yoy", "fcf_cagr_3y", "fcf_cagr_5y")):
        fy, q = fys[-1] if fys else None, qs[-1] if qs else None
        cells, ids = [], []
        for col, m, p in ((fy, mid, fy), (f"vs prior year", yoy, fy), (f"{q} vs year ago", yoy, q), ("3y CAGR", c3, fy), ("5y CAGR", c5, fy)):
            v = lk.get((m, p)) if p else None
            cells.append((col, labels.fmt(v.value, labels.kind(m)) if v and v.value is not None else "n/a"))
            if v and v.value is not None:
                ids.append(f"metric:{m}@{p}")
        rows.append({"label": labels.label(mid), "cells": cells, "ids": ids})
    return rows


def _simple_rows(lk, mids, periods):
    rows = []
    for mid in mids:
        cells, ids = [], []
        for p in periods:
            v = lk.get((mid, p))
            cells.append((p, labels.fmt(v.value, labels.kind(mid)) if v and v.value is not None else "n/a"))
            if v and v.value is not None:
                ids.append(f"metric:{mid}@{p}")
        if ids:
            rows.append({"label": labels.label(mid), "cells": cells, "ids": ids})
    return rows


def _delta(lk, mid, fys):
    if len(fys) < 2:
        return None
    a, b = lk.get((mid, fys[0])), lk.get((mid, fys[-1]))
    if a is None or b is None or a.value is None or b.value is None:
        return None
    return b.value - a.value


def _delta_text(lk, mid, fys):
    d = _delta(lk, mid, fys)
    if d is None:
        return ""
    pts = abs(d) * 100
    return f", {'up' if d >= 0 else 'down'} {pts:.1f} point{'' if round(pts, 1) == 1.0 else 's'} since {fys[0]}"


def build_note(ticker: str, snapshot: dict, metrics: list[Metric], flags, verdict: dict | None, analyses: list[dict], extras: dict | None,
               chunks_by_id: dict | None = None, filings=None, run_kind: str = "full") -> dict:
    extras = extras or {}
    lk = lookup(metrics)
    fn = Footnotes()
    cols = columns_for(metrics)
    fys = [c for c in cols if c.startswith("FY")]
    qs = [c for c in cols if c.startswith("Q")]
    fy, q = (fys[-1] if fys else None), (qs[-1] if qs else None)
    profile = extras.get("profile") or {}
    cur = {m.metric_id: m for m in metrics if m.period == "current"}

    def add(section, f):
        if f:
            f["ref"] = fn.ref(f["ids"])
            section["facts"].append(f)
        return f

    # ---- headline
    company = " · ".join(x for x in (profile.get("name"), profile.get("sector"), profile.get("industry")) if x) or ticker.upper()
    if verdict:
        composite, agreement = verdict.get("composite_score"), verdict.get("judge_agreement")
    else:
        scores = [a["grounded"]["score"] for a in analyses if a.get("grounded")]
        composite, agreement = (round(sum(scores) / len(scores), 1) if scores else None), None
    headline = {"company": company, "ticker": ticker.upper(), "price": labels.fmt(snapshot.get("latest_close"), "usd_share") if snapshot.get("latest_close") else None,
                "price_date": snapshot.get("price_date"), "market_cap": labels.fmt(cur["market_cap"].value, "usd") if "market_cap" in cur and cur["market_cap"].value else None,
                "composite": composite, "reading": reading(composite, agreement, cheap=verdict is None), "veto_note": (verdict or {}).get("veto_note"),
                "outlook": extras.get("outlook_label"), "anchors": [], "facts": []}
    rv = lk.get(("revenue_yoy", q)) if q else None
    for mid, p, tpl, tone in (("revenue_yoy", q, ("Revenue up {v} vs a year ago ({p})" if rv is not None and rv.value is not None and rv.value >= 0 else "Revenue down {v} vs a year ago ({p})"), "growth"), ("pe", "current", "P/E {v}", None), ("fcf_yield", "current", "FCF yield {v}", None)):
        f = fact(lk, mid, p, tpl, tone) if p else None
        if f and mid == "revenue_yoy":
            f["text"] = f["text"].replace("down -", "down ")
        if f:
            pct = lk.get(("pe_5y_percentile", "current"))
            if mid == "pe" and pct is not None and pct.value is not None:
                f["text"] += f" ({pct.value * 100:.0f}th percentile of its 5-year range)"
                f["ids"].append("metric:pe_5y_percentile@current")
            f["ref"] = fn.ref(f["ids"])
            headline["anchors"].append(f)

    # ---- business
    business = {"title": "Business", "facts": [], "description": (profile.get("description") or "")[:300].strip()}
    if profile.get("employees"):
        business["facts"].append({"text": f"{profile.get('name') or ticker.upper()} employs {profile['employees']:,} people.", "value": None, "ids": [], "tone": None, "flags_text": "", "ref": ""})
    segs = [s for s in extras.get("segments") or [] if s.get("share") is not None]
    top = [s for kind in ("product", "geographic") for s in sorted((x for x in segs if x["kind"] == kind), key=lambda x: -(x["share"] or 0))[:3]]
    for s in top:
        growth = f", {'growing' if (s.get('yoy') or 0) >= 0 else 'shrinking'} {abs(s['yoy']) * 100:.1f}%" if s.get("yoy") is not None else ""
        business["facts"].append({"text": f"{s['name']} is {s['share'] * 100:.0f}% of revenue ({s['kind']}, {s['fy']}){growth}.", "value": None, "ids": [], "tone": None, "flags_text": "vendor data", "ref": ""})
    if extras.get("next_earnings"):
        business["facts"].append({"text": f"Next earnings report: {extras['next_earnings']}.", "value": None, "ids": [], "tone": None, "flags_text": "", "ref": ""})

    # ---- finances
    finances = {"title": "Finances", "facts": [], "tables": {
        "growth": _growth_rows(lk, fys, qs), "margins": _simple_rows(lk, ["gross_margin", "operating_margin", "net_margin", "fcf_margin"], fys[-5:]),
        "returns": _simple_rows(lk, ["roic", "roe", "roa"], fys[-5:]), "position": _simple_rows(lk, ["cash", "total_debt", "net_debt", "net_debt_to_ebitda", "interest_coverage", "current_ratio"], ([fys[-1]] if fys else []) + ([q] if q else [])),
        "cash_quality": _simple_rows(lk, ["fcf_to_net_income", "accruals_ratio", "sbc_to_revenue", "capex_to_da"], fys[-5:]),
        "capital_allocation": _simple_rows(lk, ["net_buybacks", "dividend_coverage", "acquisitions_to_ocf", "dividend_per_share_ttm", "dividend_yield", "dividend_growth_5y", "shares_outstanding"], ([fys[-1]] if fys else []) + ["current"])}}
    if fy:
        add(finances, fact(lk, "revenue", fy, "Revenue {v} in {p}"))
        for mid in ("operating_margin", "net_margin", "roic"):
            f = fact(lk, mid, fy, "{l} {v} in {p}")
            if f:
                d = _delta(lk, mid, fys[-5:])
                f["text"] += _delta_text(lk, mid, fys[-5:])
                f["tone"] = None if d is None else "good" if d > 0 else "bad" if d < 0 else "neutral"  # the change carries the colour, not the level
                if d is not None:
                    f["ids"].append(f"metric:{mid}@{fys[-5:][0]}")
                add(finances, f)
        nd = fact(lk, "net_debt", fy, "Net debt {v} in {p}")
        prev = lk.get(("net_debt", fys[-2])) if len(fys) >= 2 else None
        if nd and prev is not None and prev.value is not None:
            cur_v = lk[("net_debt", fy)].value
            nd["text"] += f", {'up' if cur_v > prev.value else 'down'} from {labels.fmt(prev.value, 'usd')} in {fys[-2]}"
            nd["tone"] = "bad" if cur_v > prev.value else "good" if cur_v < prev.value else "neutral"
            nd["ids"].append(f"metric:net_debt@{fys[-2]}")
        add(finances, nd)
        add(finances, fact(lk, "net_debt_to_ebitda", fy, "Net debt is {v} EBITDA in {p}"))
        add(finances, fact(lk, "fcf_to_net_income", fy, "Free cash flow is {v} net income in {p}"))

    # ---- red flags
    red = {"title": "Red flags", "facts": [], "bears": []}
    for fl in flags or []:
        fid, sev, detail, ev = (fl.flag_id, fl.severity, fl.detail, fl.evidence) if hasattr(fl, "flag_id") else (fl["flag_id"], fl["severity"], fl["detail"], fl.get("evidence", []))
        red["facts"].append({"text": f"{FLAG_LABELS.get(fid, fid.replace('_', ' ').capitalize())} ({sev}): {detail}.", "value": None, "ids": list(ev), "tone": "bad", "flags_text": "", "ref": fn.ref(ev)})
    if verdict:
        bears = [c for c in verdict.get("consensus_claims", []) if c["direction"] == "negative"] + [c for c in verdict.get("contested_claims", []) if c["direction"] == "negative"]
    else:
        bears = sorted((dict(c, agent=a["agent"]) for a in analyses for c in (a.get("grounded") or {}).get("claims", []) if c["direction"] == "negative"), key=lambda c: -c.get("weight", 0))
    red["bears"] = [_claim_view(c, fn) for c in bears[:3]]  # the full bear case is in The debate

    # ---- strategy
    strategy = {"title": "Strategy", "facts": [], "narrative": (extras.get("narrative") or {}).get("one_line")}
    gd = next((m for m in metrics if m.metric_id == "guidance_direction" and m.value is not None), None)
    if gd:
        word = "raised" if gd.value > 0 else "lowered" if gd.value < 0 else "held"
        quote = (gd.inputs or {}).get("quote") or (gd.inputs or {}).get("statement")
        strategy["facts"].append({"text": f"Guidance was {word} on the latest call ({gd.period})" + (f': "{str(quote)[:160]}"' if quote else "") + ".", "value": None, "ids": [f"metric:guidance_direction@{gd.period}"], "tone": None, "flags_text": "", "ref": fn.ref([f"metric:guidance_direction@{gd.period}"])})
    mt = next((m for m in metrics if m.metric_id == "management_tone" and m.value is not None), None)
    if mt:
        word = "confident" if mt.value > 0 else "cautious" if mt.value < 0 else "neutral"
        strategy["facts"].append({"text": f"Management tone on the latest call was {word} ({mt.period}).", "value": None, "ids": [f"metric:management_tone@{mt.period}"], "tone": None, "flags_text": "", "ref": fn.ref([f"metric:management_tone@{mt.period}"])})
    add(strategy, fact(lk, "eps_estimate_revision_90d", "current", "Consensus EPS estimates moved {v} over 90 days", "growth"))
    add(strategy, fact(lk, "capex_to_da", fy, "Capex is {v} depreciation in {p}") if fy else None)
    add(strategy, fact(lk, "acquisitions_to_ocf", fy, "Acquisitions took {v} of operating cash flow in {p}") if fy else None)
    add(strategy, fact(lk, "insider_net_shares_90d", "current", "Insiders net {v} over 90 days"))
    add(strategy, fact(lk, "inst_shares_change_pct", "current", "Institutional holdings changed {v} last quarter"))
    for c in (verdict or {}).get("catalysts", [])[:4]:
        strategy["facts"].append({"text": f"Catalyst: {c['what']} ({c['when']}).", "value": None, "ids": list(c.get("evidence", [])), "tone": None, "flags_text": "", "ref": fn.ref(c.get("evidence", []))})

    # ---- valuation
    valuation = {"title": "Valuation", "facts": []}
    for mid, lab in (("pe", "P/E"), ("ev_ebitda", "EV/EBITDA"), ("ev_sales", "EV/sales"), ("fcf_yield", "FCF yield")):
        m = lk.get((mid, "current"))
        if m is None or m.value is None:
            continue
        k = labels.kind(mid)
        parts, ids = [f"{lab} {labels.fmt(m.value, k)}"], [f"metric:{mid}@current"]
        lo, med, hi, pct = (lk.get((f"{mid}_5y_{s}", "current")) for s in ("min", "median", "max", "percentile"))
        if med is not None and med.value is not None:
            have_range = lo is not None and hi is not None and lo.value is not None and hi.value is not None
            rng = f" (5-year range {labels.fmt(lo.value, k)}–{labels.fmt(hi.value, k)})" if have_range else ""
            parts.append(f"against a 5-year median of {labels.fmt(med.value, k)}{rng}" + (f", {pct.value * 100:.0f}th percentile" if pct is not None and pct.value is not None else ""))
            ids += [f"metric:{mid}_5y_median@current"] + ([f"metric:{mid}_5y_min@current", f"metric:{mid}_5y_max@current"] if have_range else []) + ([f"metric:{mid}_5y_percentile@current"] if pct is not None and pct.value is not None else [])
        rel = lk.get((f"{mid}_vs_peer_median", "current"))
        pm = lk.get((f"{mid}_peer_median", "current"))
        if rel is not None and rel.value is not None and pm is not None and pm.value is not None:
            parts.append(f"and {rel.value:.2f}x the peer median of {labels.fmt(pm.value, k)}")
            ids += [f"metric:{mid}_vs_peer_median@current", f"metric:{mid}_peer_median@current"]
        valuation["facts"].append({"text": ", ".join(parts[:1] + ([" ".join(parts[1:])] if len(parts) > 1 else [])) + ".", "value": labels.fmt(m.value, k), "ids": ids, "tone": None, "flags_text": labels.flag_words(m.flags), "ref": fn.ref(ids)})
    add(valuation, fact(lk, "forward_pe", "current", "Forward P/E {v} on consensus EPS"))
    dcf = {s: lk.get((f"dcf_{s}", "current")) for s in ("bear", "base", "bull")}
    if all(v is not None and v.value is not None for v in dcf.values()):
        price = snapshot.get("latest_close")
        vs = f" against a price of {labels.fmt(price, 'usd_share')}" if price else ""
        valuation["facts"].append({"text": f"DCF value {labels.fmt(dcf['bear'].value, 'usd_share')} (bear) to {labels.fmt(dcf['bull'].value, 'usd_share')} (bull), base {labels.fmt(dcf['base'].value, 'usd_share')}{vs}.",
                                   "value": None, "ids": [f"metric:dcf_{s}@current" for s in ("bear", "base", "bull")], "tone": None, "flags_text": "", "ref": fn.ref([f"metric:dcf_{s}@current" for s in ("bear", "base", "bull")])})
    add(valuation, fact(lk, "reverse_dcf_implied_growth", "current", "The price implies {v} annual FCF growth"))
    add(valuation, fact(lk, "price_target_upside", "current", "Analyst targets imply {v} upside (vendor consensus)"))
    r = extras.get("ratings")
    if r:
        valuation["facts"].append({"text": f"Analyst ratings ({r['date']}): {r['strong_buy']} strong buy, {r['buy']} buy, {r['hold']} hold, {r['sell']} sell, {r['strong_sell']} strong sell.", "value": None, "ids": [], "tone": None, "flags_text": "vendor data", "ref": ""})

    # ---- the debate
    if verdict:
        bull = [c for c in verdict.get("consensus_claims", []) if c["direction"] == "positive"][:5]
        bear = [c for c in verdict.get("consensus_claims", []) if c["direction"] == "negative"][:5]
        contested = verdict.get("contested_claims", [])[:2]
        subs = {k: verdict.get(k) for k in ("quality_score", "value_score", "earnings_momentum_score", "accounting_risk_score")}
        joined = ", ".join(f"{k.replace('_score', '').replace('_', ' ')} {v}" for k, v in subs.items() if v is not None)
        subscores_text = joined + " (0–100)" if joined else ""
        debate = {"title": "The debate", "bull": [_claim_view(c, fn) for c in bull], "bear": [_claim_view(c, fn) for c in bear], "contested": [_claim_view(c, fn) for c in contested],
                  "summary": verdict.get("one_paragraph_summary") or None, "median_judge": verdict.get("median_judge"), "subscores_text": subscores_text}
    else:
        allc = [dict(c, agent=a["agent"], id=f"{a['agent']}#{i}") for a in analyses for i, c in enumerate((a.get("grounded") or {}).get("claims", []))]
        bull = sorted((c for c in allc if c["direction"] == "positive"), key=lambda c: -c.get("weight", 0))[:5]
        bear = sorted((c for c in allc if c["direction"] == "negative"), key=lambda c: -c.get("weight", 0))[:5]
        debate = {"title": "The debate", "bull": [_claim_view(c, fn) for c in bull], "bear": [_claim_view(c, fn) for c in bear], "contested": [], "summary": None, "median_judge": None, "subscores_text": ""}

    # ---- appendix
    run_details = []
    if verdict:
        run_details.append("Providers: " + ", ".join(verdict.get("providers", [])) + ".")
        run_details.append("Screen panel: " + (", ".join(f"{j['provider']}/{j['model']}" for j in verdict.get("screen_judges") or []) or "none") + "; escalation panel: " + (", ".join(f"{j['provider']}/{j['model']}" for j in verdict.get("judges") or []) or "none") + ".")
        if verdict.get("n_claims") is not None:
            run_details.append(f"{len(verdict.get('escalated_ids') or [])} of {verdict['n_claims']} claims escalated to the strong panel." + (f" {len(verdict['fallback_ids'])} escalated claims were unscored by every strong judge; the screen scores stand." if verdict.get("fallback_ids") else ""))
        run_details.append(f"Composite {verdict.get('composite_score')} ({verdict.get('weighting')}; uncapped {verdict.get('composite_uncapped')}), dispersion {verdict.get('dispersion')}, judge agreement {verdict.get('judge_agreement')}; implied composites " + ", ".join(f"{p} {v}" for p, v in (verdict.get("implied_composites") or {}).items()) + ".")
        for p, text in (verdict.get("summaries") or {}).items():
            if p != verdict.get("median_judge"):
                run_details.append(f"Judge {p}: {text}")
        for p, nums in (verdict.get("summary_unsupported") or {}).items():
            if nums:
                run_details.append(f"Unsupported numbers in {p}'s summary: {', '.join(nums)}.")
    for a in analyses:
        st = (a.get("grounded") or {}).get("stats") or {}
        if st:
            run_details.append(f"{a['agent']}: {st.get('claims_total')} claims, {st.get('claims_dropped')} dropped, {st.get('numbers_unsupported')}/{st.get('numbers_total')} numbers unsupported.")
    metrics_table = []
    for group, ids in GROUPS.items():
        rows = [(mid, [table_fmt(lk[(mid, c)]) if (mid, c) in lk else "" for c in cols]) for mid in ids]
        rows = [rw for rw in rows if any(rw[1])]
        if rows:
            metrics_table.append((group, rows))
    seg_rows = [(m.metric_id, [table_fmt(lk[(m.metric_id, c)]) if (m.metric_id, c) in lk else "" for c in cols]) for m in sorted({m.metric_id: m for m in metrics if m.metric_id.startswith("seg_")}.values(), key=lambda m: m.metric_id)]
    if seg_rows:
        metrics_table.append(("segments (FMP)", seg_rows))
    appendix = {"run_details": " ".join(run_details), "grid": (verdict or {}).get("grid"), "dispersion_by_persona": (verdict or {}).get("dispersion_by_persona"), "dispersion_by_provider": (verdict or {}).get("dispersion_by_provider"),
                "providers": (verdict or {}).get("providers") or sorted({a["provider"] for a in analyses}), "revised_scores": (verdict or {}).get("revised_scores"), "agent_weights": (verdict or {}).get("agent_weights"),
                "personas": analyses, "metrics_table": metrics_table, "columns": cols, "dcf": {"discount_rate": DISCOUNT_RATE, "terminal_growth": TERMINAL_GROWTH, "years": YEARS},
                "crosschecks": {"fmp": extras.get("crosscheck"), "edgar": extras.get("edgar_crosscheck")}, "sources": [],
                "judged": [_claim_view(c, fn) for c in ((verdict or {}).get("consensus_claims", []) + (verdict or {}).get("contested_claims", []))]}
    appendix["sources"] = fn.entries(metrics, chunks_by_id, filings)
    return {"ticker": ticker.upper(), "run_kind": run_kind, "snapshot": snapshot, "headline": headline, "business": business, "finances": finances, "red_flags": red,
            "strategy": strategy, "valuation": valuation, "debate": debate, "appendix": appendix, "footnotes": fn}


def load_extras(conn, cik: int, ticker: str) -> dict:
    """Stored rows only: never a fetch. The pipeline calls this and hands the result to the renderers."""
    from fundabot.analytics import moves
    from fundabot.ingest import fmp_extra

    never = 10**6
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM ticker_facts WHERE (cik = %s AND key IN ('next_earnings', 'ratings', 'fmp_crosscheck', 'edgar_crosscheck', 'scores')) OR (cik = 0 AND key = 'treasury_10y')", (cik,))
        facts = dict(cur.fetchall())
    row = moves.load(conn, cik, 90)
    return {"profile": fmp_extra.load_profile(conn, cik), "segments": _segment_rows(conn, cik), "peers": fmp_extra.load_peers(conn, cik), "dividends": {},
            "holders": fmp_extra.load_holders(conn, cik), "insider_trades": fmp_extra.load_insider_trades(conn, cik, limit=12), "scores": facts.get("scores"), "ratings": facts.get("ratings"),
            "next_earnings": facts.get("next_earnings"), "crosscheck": facts.get("fmp_crosscheck"), "edgar_crosscheck": facts.get("edgar_crosscheck"),
            "narrative": row["output"] if row else None, "risk_free": facts.get("treasury_10y")}


def _segment_rows(conn, cik: int) -> list[dict]:
    """Latest two fiscal years per segment from the stored segments table, in the shape views.segment_table uses."""
    from fundabot.ingest import fmp_extra

    rows = fmp_extra.load_segments(conn, cik)
    out = []
    for kind in ("product", "geographic"):
        k = [r for r in rows if r["kind"] == kind and r.get("revenue") is not None]
        if not k:
            continue
        latest = max(r["fiscal_year"] for r in k)
        total = sum(r["revenue"] for r in k if r["fiscal_year"] == latest) or None
        prev_total = sum(r["revenue"] for r in k if r["fiscal_year"] == latest - 1) or None
        prev = {r["name"]: r["revenue"] for r in k if r["fiscal_year"] == latest - 1}
        for r in sorted((r for r in k if r["fiscal_year"] == latest), key=lambda r: -r["revenue"])[:8 if kind == "product" else 6]:
            out.append({"kind": kind, "name": r["name"], "fy": f"FY{latest}", "share": (r["revenue"] / total) if total else None, "yoy": (r["revenue"] / prev[r["name"]] - 1) if prev.get(r["name"]) else None,
                        "revenue": r["revenue"], "prev_share": (prev[r["name"]] / prev_total) if prev.get(r["name"]) and prev_total else None})
    return out
