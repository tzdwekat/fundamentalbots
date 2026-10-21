"""Render the analyst note (full and cheap runs) from stored data; see fundabot/report/note.py."""
import re
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader

from fundabot.analytics.metrics import Metric
from fundabot.analytics.valuation import DISCOUNT_RATE, TERMINAL_GROWTH, YEARS
from fundabot.report import charts
from fundabot.retrieval.chunk import Chunk

ENV = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=False)
CSS = "body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;line-height:1.45}" \
      "table{border-collapse:collapse;font-size:13px;margin:1em 0}td,th{border:1px solid #ccc;padding:3px 6px;text-align:right}" \
      "th:first-child,td:first-child{text-align:left}code{background:#f4f4f4;padding:1px 3px}blockquote{color:#a40;border-left:3px solid #a40;padding-left:.6em}" \
      "a.fn{font-size:11px;vertical-align:super;text-decoration:none;color:#2a78d6}details{margin:.6em 0}summary{cursor:pointer;font-weight:600}h2{margin-top:1.4em}" + charts.VIZ_CSS


def _footnote_links(md: str) -> str:
    """[1,2] markers in the note body become anchors into the Sources list; the appendix (quoted text, the list itself) is left alone."""
    body, sep, appendix = md.partition("# Appendix")
    linked = re.sub(r"\[(\d+(?:,\d+)*)\]", lambda m: " ".join(f'<a class="fn" href="#src-{n}">[{n}]</a>' for n in m.group(1).split(",")), body)
    return linked + sep + appendix


def _render_note(ticker, snapshot, metrics, flags, verdict, analyses, chunks_by_id, filings, out_dir: Path, run_kind: str, extras: dict | None, suffix: str) -> tuple[Path, Path]:
    from fundabot.agents.runner import PERSONA_ORDER
    from fundabot.report.note import build_note

    n = build_note(ticker, snapshot, metrics, flags, verdict, analyses, extras, chunks_by_id, filings, run_kind=run_kind)
    n["charts_html"] = " ".join(line.strip() for line in chart_block(metrics).splitlines())  # one line: the note's prose stays readable in Markdown
    n["personas"] = [p for p in PERSONA_ORDER if any(a["persona"] == p for a in analyses)]
    heat = ""
    if verdict and verdict.get("grid"):
        cells = {tuple(k.split("/")): (c.get("score") if c else None) for k, c in verdict["grid"].items()}
        for p in n["personas"]:
            for pr in verdict.get("providers", []):
                cells.setdefault((p, pr), None)
        heat = '<div class="viz-root">' + charts.heatmap(n["personas"], verdict.get("providers", []), cells) + "</div>"
    n["heatmap_html"] = heat
    md = ENV.get_template("note.md.j2").render(n=n)
    out = out_dir / ticker.upper()
    out.mkdir(parents=True, exist_ok=True)
    md_path, html_path = out / f"{snapshot['id']}{suffix}.md", out / f"{snapshot['id']}{suffix}.html"
    md_path.write_text(md)
    body = markdown.markdown(_footnote_links(md), extensions=["tables", "md_in_html"])
    html_path.write_text(f"<!doctype html><html><head><meta charset='utf-8'><title>{ticker.upper()} {'verdict' if verdict else 'fundamentals'}</title><style>{CSS}</style></head><body>{body}</body></html>")
    return md_path, html_path


def render_verdict(ticker, snapshot, verdict, analyses, rebuttals, metrics, flags, chunks_by_id, filings, out_dir: Path, run_kind: str = "full", extras: dict | None = None) -> tuple[Path, Path]:
    """The analyst note for a full run: headline, business, finances, red flags, strategy, valuation, the debate, appendix."""
    by_agent = {r["agent"]: r["output"] for r in rebuttals}
    for a in analyses:
        a["rebuttal"] = by_agent.get(a["agent"])
    return _render_note(ticker, snapshot, metrics, flags, verdict, analyses, chunks_by_id, filings, out_dir, run_kind, extras, f"-{run_kind}")


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
                  filings: list[dict], out_dir: Path, extras: dict | None = None) -> tuple[Path, Path]:
    """The analyst note for a cheap run (no verdict): the debate comes from the persona claims."""
    for a in analyses:
        a.setdefault("rebuttal", None)
    return _render_note(ticker, snapshot, metrics, flags, None, analyses, chunks_by_id, filings, out_dir, "cheap", extras, "")
