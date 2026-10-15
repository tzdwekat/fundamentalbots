"""Render the verdict-less Phase 2 report: scores, per-persona theses with grounding, metrics, sources."""
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader

from fundabot.analytics.metrics import Metric
from fundabot.analytics.valuation import DISCOUNT_RATE, TERMINAL_GROWTH, YEARS
from fundabot.report.table import GROUPS, columns_for, fmt
from fundabot.retrieval.chunk import Chunk

ENV = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=False)
CSS = "body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;line-height:1.45}" \
      "table{border-collapse:collapse;font-size:13px;margin:1em 0}td,th{border:1px solid #ccc;padding:3px 6px;text-align:right}" \
      "th:first-child,td:first-child{text-align:left}code{background:#f4f4f4;padding:1px 3px}blockquote{color:#a40;border-left:3px solid #a40;padding-left:.6em}"


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
        ticker=ticker, snapshot=snapshot, filings=filings, flags=flags, analyses=analyses,
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
