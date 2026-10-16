"""Inline SVG charts for the reports. Pure functions, no dependencies, colors by CSS token (see VIZ_CSS).

Forms: line_chart (change over time), range_bar (a value against its own range), heatmap (persona x provider scores,
diverging). Marks follow the data-viz method: 2px lines, >= 8px markers with a surface ring, hairline gridlines,
legend for >= 2 series, selective end labels, text in ink tokens never series colors, a <title> tooltip per mark.
"""
from html import escape

W, H, PAD_L, PAD_R, PAD_T, PAD_B = 640, 220, 56, 72, 28, 36
SERIES = 4  # categorical slots used (validated all-adjacent in both modes)

VIZ_CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;--grid:#e6e5e1;--track:#e6e5e1;
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--mid:#f0efec;
--pos-1:#cde2fb;--pos-2:#86b6ef;--pos-3:#3987e5;--pos-4:#1c5cab;--neg-1:#f9d4d3;--neg-2:#f0a3a2;--neg-3:#e34948;--neg-4:#a32626}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]) .viz-root{color-scheme:dark;--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;
--grid:#383835;--track:#383835;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--mid:#383835;
--pos-1:#184f95;--pos-2:#256abf;--pos-3:#3987e5;--pos-4:#86b6ef;--neg-1:#6b1f1f;--neg-2:#a32626;--neg-3:#e34948;--neg-4:#f0a3a2}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;
--grid:#383835;--track:#383835;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--mid:#383835;
--pos-1:#184f95;--pos-2:#256abf;--pos-3:#3987e5;--pos-4:#86b6ef;--neg-1:#6b1f1f;--neg-2:#a32626;--neg-3:#e34948;--neg-4:#f0a3a2}
.viz-root svg{display:block;max-width:100%;height:auto;background:var(--surface-1);font-family:-apple-system,Helvetica,Arial,sans-serif;margin:12px 0}
.viz-root text{fill:var(--text-primary);font-size:12px}.viz-root .muted{fill:var(--text-secondary);font-size:11px}
.viz-root .title{font-size:13px;font-weight:600}.viz-root .grid{stroke:var(--grid);stroke-width:1}
.viz-root .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.viz-root .marker{stroke:var(--surface-1);stroke-width:2}.viz-root .track{fill:var(--track)}
.viz-root .cell-label{font-size:12px;font-weight:600}
"""


def fmt_value(v: float | None, fmt: str) -> str:
    if v is None:
        return "n/a"
    if fmt == "pct":
        return f"{v * 100:.1f}%"
    if fmt == "x":
        return f"{v:.1f}x"
    if fmt == "usd":
        a = abs(v)
        sign = "-" if v < 0 else ""
        if a >= 1e12:
            return f"{sign}${a / 1e12:.1f}T"
        if a >= 1e9:
            return f"{sign}${a / 1e9:.1f}B"
        if a >= 1e6:
            return f"{sign}${a / 1e6:.1f}M"
        return f"{sign}${a:,.0f}"
    return f"{v:,.2f}"


def _ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    import math
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    start = math.floor(lo / step) * step
    return [start + i * step for i in range(int((hi - start) / step) + 2)]


def line_chart(title: str, series: list[tuple[str, list[tuple[str, float | None]]]], fmt: str = "usd") -> str:
    """series: [(label, [(x_label, value | None), ...])]; all series share the x labels of the first. None breaks the line."""
    xs = [x for x, _ in series[0][1]] if series else []
    vals = [v for _, pts in series for _, v in pts if v is not None]
    if not xs or not vals:
        return f'<svg class="chart" width="{W}" height="80" viewBox="0 0 {W} 80" role="img" aria-label="{escape(title)}"><text x="12" y="24" class="title">{escape(title)}</text><text x="12" y="52" class="muted">no data</text></svg>'
    lo, hi = min(vals + [0]) if fmt != "x" else min(vals), max(vals)
    ticks = _ticks(lo, hi)
    lo, hi = ticks[0], ticks[-1]
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def sx(i):
        return PAD_L + (i * plot_w / max(1, len(xs) - 1))

    def sy(v):
        return PAD_T + plot_h - (v - lo) / (hi - lo) * plot_h

    out = [f'<svg class="chart" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}">',
           f'<text x="12" y="18" class="title">{escape(title)}</text>']
    for t in ticks:
        y = sy(t)
        out.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}"/>')
        out.append(f'<text x="{PAD_L - 6}" y="{y + 4:.1f}" class="muted" text-anchor="end">{escape(fmt_value(t, fmt))}</text>')
    step = max(1, len(xs) // 8)
    for i, x in enumerate(xs):
        if i % step == 0 or i == len(xs) - 1:
            out.append(f'<text x="{sx(i):.1f}" y="{H - 14}" class="muted" text-anchor="middle">{escape(x)}</text>')
    for si, (label, pts) in enumerate(series[:SERIES]):
        color = f"var(--series-{si + 1})"
        segment = []
        last = None
        for i, (_, v) in enumerate(pts):
            if v is None:
                if len(segment) > 1:
                    out.append(f'<polyline class="line" stroke="{color}" points="{" ".join(segment)}"/>')
                segment = []
                continue
            segment.append(f"{sx(i):.1f},{sy(v):.1f}")
            last = (i, v)
        if len(segment) > 1:
            out.append(f'<polyline class="line" stroke="{color}" points="{" ".join(segment)}"/>')
        for i, (x, v) in enumerate(pts):
            if v is not None:
                out.append(f'<circle class="marker" cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="4" fill="{color}"><title>{escape(label)} {escape(x)}: {escape(fmt_value(v, fmt))}</title></circle>')
        if last is not None:
            out.append(f'<text x="{sx(last[0]) + 8:.1f}" y="{sy(last[1]) + 4:.1f}">{escape(fmt_value(last[1], fmt))}</text>')
    if len(series) > 1:
        x = PAD_L
        out.append('<g class="legend">')
        for si, (label, _) in enumerate(series[:SERIES]):
            out.append(f'<rect x="{x}" y="{PAD_T - 16}" width="10" height="10" rx="2" fill="var(--series-{si + 1})"/><text x="{x + 14}" y="{PAD_T - 7}" class="muted">{escape(label)}</text>')
            x += 14 + 7 * len(label) + 18
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


def range_bar(label: str, lo: float | None, median: float | None, hi: float | None, current: float | None, fmt: str = "x") -> str:
    """The current value marked on its own historical range (lo..hi) with the median; values outside are clamped and labelled."""
    h = 44
    if lo is None or hi is None or current is None:
        return f'<svg class="chart" width="{W}" height="{h}" viewBox="0 0 {W} {h}" role="img" aria-label="{escape(label)}"><text x="12" y="27">{escape(label)}</text><text x="{PAD_L + 80}" y="27" class="muted">n/a (no history or no current value)</text></svg>'
    x0, x1 = 150, W - 90
    span = (hi - lo) or 1.0

    def px(v):
        return x0 + max(0.0, min(1.0, (v - lo) / span)) * (x1 - x0)
    out = [f'<svg class="chart" width="{W}" height="{h}" viewBox="0 0 {W} {h}" role="img" aria-label="{escape(label)}">',
           f'<text x="12" y="27">{escape(label)}</text>',
           f'<rect class="track" x="{x0}" y="18" width="{x1 - x0}" height="8" rx="4"/>',
           f'<text x="{x0 - 6}" y="27" class="muted" text-anchor="end">{escape(fmt_value(lo, fmt))}</text>',
           f'<text x="{x1 + 6}" y="27" class="muted">{escape(fmt_value(hi, fmt))}</text>']
    if median is not None:
        out.append(f'<circle class="median" cx="{px(median):.1f}" cy="22" r="4" fill="var(--text-secondary)"><title>5y median {escape(fmt_value(median, fmt))}</title></circle>')
    out.append(f'<circle class="marker current" cx="{px(current):.1f}" cy="22" r="6" fill="var(--series-1)"><title>current {escape(fmt_value(current, fmt))}</title></circle>')
    out.append(f'<text x="{px(current):.1f}" y="12" text-anchor="middle">{escape(fmt_value(current, fmt))}{"" if lo <= current <= hi else " (outside range)"}</text>')
    out.append("</svg>")
    return "\n".join(out)


def _bucket(score: float | None) -> str:
    if score is None:
        return "var(--mid)"
    a = min(4, max(1, int((abs(score) - 1) // 2.5) + 1)) if abs(score) >= 1 else 0
    if a == 0:
        return "var(--mid)"
    return f"var(--{'pos' if score > 0 else 'neg'}-{a})"


def heatmap(personas: list[str], providers: list[str], cells: dict[tuple[str, str], float | None]) -> str:
    """Persona x provider scores on a blue-red diverging ramp with a gray midpoint; failed cells say so."""
    cw, ch, left, top = 110, 34, 90, 30
    w, h = left + cw * len(providers) + 20, top + ch * len(personas) + 40
    out = [f'<svg class="chart" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="Persona by provider score grid">']
    for j, pr in enumerate(providers):
        out.append(f'<text x="{left + j * cw + cw / 2:.0f}" y="20" class="muted" text-anchor="middle">{escape(pr)}</text>')
    for i, p in enumerate(personas):
        out.append(f'<text x="{left - 8}" y="{top + i * ch + ch / 2 + 4:.0f}" text-anchor="end">{escape(p)}</text>')
        for j, pr in enumerate(providers):
            v = cells.get((p, pr))
            x, y = left + j * cw + 1, top + i * ch + 1
            fill = _bucket(v)
            dark_text = v is None or abs(v) < 3.5
            label = "failed" if v is None else f"{v:g}"
            out.append(f'<rect class="cell" x="{x}" y="{y}" width="{cw - 2}" height="{ch - 2}" rx="4" fill="{fill}"><title>{escape(p)}/{escape(pr)}: {escape(label)}</title></rect>')
            out.append(f'<text class="cell-label" x="{x + (cw - 2) / 2:.0f}" y="{y + ch / 2 + 3:.0f}" text-anchor="middle" style="fill:{"var(--text-primary)" if dark_text else "#ffffff"}">{escape(label)}</text>')
    y = top + ch * len(personas) + 22
    out.append(f'<text x="{left}" y="{y}" class="muted">score: -10 (avoid)</text>')
    for k, tok in enumerate(["--neg-4", "--neg-3", "--neg-2", "--neg-1", "--mid", "--pos-1", "--pos-2", "--pos-3", "--pos-4"]):
        out.append(f'<rect x="{left + 120 + k * 14}" y="{y - 10}" width="12" height="12" rx="2" fill="var({tok})"/>')
    out.append(f'<text x="{left + 120 + 9 * 14 + 6}" y="{y}" class="muted">+10 (own)</text></svg>')
    return "\n".join(out)
