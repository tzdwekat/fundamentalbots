"""Render the verdict-less Phase 2 report: scores, per-persona theses with grounding, metrics, sources."""
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader

from fundabot.analytics.metrics import Metric
from fundabot.analytics.valuation import DISCOUNT_RATE, TERMINAL_GROWTH, YEARS
from fundabot.report import charts
from fundabot.report.table import GROUPS, columns_for, fmt
from fundabot.retrieval.chunk import Chunk

ENV = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=False)
CSS = "body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;line-height:1.45}" \
      "table{border-collapse:collapse;font-size:13px;margin:1em 0}td,th{border:1px solid #ccc;padding:3px 6px;text-align:right}" \
      "th:first-child,td:first-child{text-align:left}code{background:#f4f4f4;padding:1px 3px}blockquote{color:#a40;border-left:3px solid #a40;padding-left:.6em}" + charts.VIZ_CSS


def render_verdict(ticker, snapshot, verdict, analyses, rebuttals, metrics, flags, chunks_by_id, filings, out_dir: Path) -> tuple[Path, Path]:
    """Verdict sections, then the per-agent report (15 agents with their rebuttals) and the sources appendix."""
    from fundabot.agents.context import metric_line
    from fundabot.agents.runner import PERSONA_ORDER

    earnings_lines = [metric_line(m) for m in metrics if ("vendor_fmp" in m.flags or "llm_extracted" in m.flags) and m.value is not None]
    cells = {tuple(k.split("/")): (c.get("score") if c else None) for k, c in verdict["grid"].items()}
    for p in PERSONA_ORDER:
        for pr in verdict["providers"]:
            cells.setdefault((p, pr), None)
    heat = '<div class="viz-root">' + charts.heatmap([p for p in PERSONA_ORDER if any(k[0] == p for k in cells)], verdict["providers"], cells) + "</div>"
    head = ENV.get_template("verdict.md.j2").render(ticker=ticker.upper(), snapshot=snapshot, verdict=verdict, personas=PERSONA_ORDER, earnings_lines=earnings_lines,
                                                    heatmap_html=heat)
    by_agent = {r["agent"]: r["output"] for r in rebuttals}
    for a in analyses:
        a["rebuttal"] = by_agent.get(a["agent"])
    body_md, _ = render_report(ticker, snapshot, metrics, flags, analyses, chunks_by_id, filings, out_dir)
    body = body_md.read_text().split("\n", 1)[1]
    body_md.unlink()
    body_md.with_suffix(".html").unlink(missing_ok=True)
    md = head + "\n---\n# Agent detail\n" + body
    out = out_dir / ticker.upper()
    md_path, html_path = out / f"{snapshot['id']}-full.md", out / f"{snapshot['id']}-full.html"
    md_path.write_text(md)
    html_path.write_text(f"<!doctype html><html><head><meta charset='utf-8'><title>{ticker.upper()} verdict</title><style>{CSS}</style></head><body>"
                         f"{markdown.markdown(md, extensions=['tables'])}</body></html>")
    return md_path, html_path


def chart_block(metrics: list[Metric]) -> str:
    """Trend lines, growth, and valuation range bars from the metrics table; wrapped for the viz tokens."""
    lookup = {(m.metric_id, m.period): m for m in metrics}
    fys = sorted({m.period for m in metrics if m.period.startswith("FY")})[-6:]
    qs = sorted({m.period for m in metrics if m.period.startswith("Q")}, key=lambda p: (int(p[4:]), int(p[1])))[-8:]

    def pts(mid, periods):
        return [(p, lookup[(mid, p)].value if (mid, p) in lookup else None) for p in periods]
    parts = ['<div class="viz-root">',
             charts.line_chart("Revenue and free cash flow (fiscal years)", [("revenue", pts("revenue", fys)), ("fcf", pts("fcf", fys))], "usd"),
             charts.line_chart("Margins and return on capital", [("gross margin", pts("gross_margin", fys)), ("operating margin", pts("operating_margin", fys)),
                                                                 ("FCF margin", pts("fcf_margin", fys)), ("ROIC", pts("roic", fys))], "pct"),
             charts.line_chart("Revenue growth, year over year (last 8 quarters)", [("revenue YoY", pts("revenue_yoy", qs))], "pct")]
    parts.append('<p class="title" style="margin:16px 0 4px;font-weight:600">Valuation against its own 5-year range</p>')
    for mid, label, f in (("pe", "P/E (TTM)", "x"), ("ev_ebitda", "EV / EBITDA", "x"), ("ev_sales", "EV / sales", "x"), ("fcf_yield", "FCF yield", "pct")):
        g = lambda k: lookup[(f"{mid}_{k}", "current")].value if (f"{mid}_{k}", "current") in lookup else None
        parts.append(charts.range_bar(label, g("5y_min"), g("5y_median"), g("5y_max"), lookup[(mid, "current")].value if (mid, "current") in lookup else None, f))
    parts.append('<p class="muted">Valuation bars: the current value marked on the ratio\'s own 5-year range (dot = 5-year median). Values are in the metrics table below.</p></div>')
    return "\n".join(parts)


def _cited_ids(analyses: list[dict]) -> tuple[set[str], set[str]]:
    metric_ids, chunk_ids = set(), set()
    for a in analyses:
        for c in a["grounded"]["claims"]:
            for e in c["evidence"]:
                (metric_ids if e.startswith("metric:") else chunk_ids).add(e.split(":", 1)[1])
    return metric_ids, chunk_ids


def render_report(ticker: str, snapshot: dict, metrics: list[Metric], flags, analyses: list[dict], chunks_by_id: dict[str, Chunk],
                  filings: list[dict], out_dir: Path) -> tuple[Path, Path]:
    cols = columns_for(metrics)
    lookup = {(m.metric_id, m.period): m for m in metrics}
    groups = []
    for group, ids in GROUPS.items():
        rows = [(mid, [fmt(lookup[(mid, c)]) if (mid, c) in lookup else "" for c in cols]) for mid in ids]
        rows = [r for r in rows if any(r[1])]
        if rows:
            groups.append((group, rows))
    metric_ids, chunk_ids = _cited_ids(analyses)
    cited_metrics = sorted((m for m in metrics if m.metric_id in {i.split("@")[0] for i in metric_ids}
                            and (f"{m.metric_id}@{m.period}" in metric_ids or m.metric_id in metric_ids)), key=lambda m: (m.metric_id, m.period))
    cited_chunks = [chunks_by_id[c] for c in sorted(chunk_ids) if c in chunks_by_id]
    scores = [a["grounded"]["score"] for a in analyses]
    md = ENV.get_template("report.md.j2").render(
        ticker=ticker, snapshot=snapshot, filings=filings, flags=flags, analyses=analyses, charts_html=chart_block(metrics),
        mean_score=sum(scores) / len(scores) if scores else 0.0, columns=cols, metric_groups=groups,
        dcf={"discount_rate": DISCOUNT_RATE, "terminal_growth": TERMINAL_GROWTH, "years": YEARS},
        cited_metrics=cited_metrics, cited_chunks=cited_chunks,
    )
    out = out_dir / ticker.upper()
    out.mkdir(parents=True, exist_ok=True)
    md_path, html_path = out / f"{snapshot['id']}.md", out / f"{snapshot['id']}.html"
    md_path.write_text(md)
    body = markdown.markdown(md, extensions=["tables"])
    html_path.write_text(f"<!doctype html><html><head><meta charset='utf-8'><title>{ticker.upper()} fundamentals</title><style>{CSS}</style></head><body>{body}</body></html>")
    return md_path, html_path
