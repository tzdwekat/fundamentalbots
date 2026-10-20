"""Metrics from FMP datasets, computed in code from stored rows and flagged vendor_fmp: peer medians and relatives,
revenue segments (share, growth), dividends (TTM, yield, 5y growth), Altman Z / Piotroski, the 10-year yield."""
import re
from datetime import date, timedelta
from statistics import median

from fundabot.analytics.metrics import Metric

MAX_PRODUCT, MAX_GEO = 8, 6
FIXED = {"pe_peer_median": "peers:median", "ev_sales_peer_median": "peers:median", "ev_ebitda_peer_median": "peers:median", "pe_vs_peer_median": "peers:median",
         "ev_sales_vs_peer_median": "peers:median", "dividend_per_share_ttm": "fmp:dividends", "dividend_yield": "fmp:dividends", "dividend_growth_5y": "fmp:dividends",
         "altman_z": "fmp:financial-scores", "piotroski": "fmp:financial-scores", "risk_free_10y": "fmp:treasury-rates"}
UNITS = {"dividend_per_share_ttm": "USD/share", "dividend_yield": "ratio", "dividend_growth_5y": "ratio", "altman_z": "score", "piotroski": "score", "risk_free_10y": "ratio"}


def _m(mid: str, period: str, value, formula: str, inputs: dict | None = None, reason: str | None = None, unit: str | None = None) -> Metric:
    return Metric(mid, period, value, unit or UNITS.get(mid, "ratio"), formula, inputs or {}, ["vendor_fmp"], None, None if value is not None else (reason or "no_coverage"))


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    s = re.sub(r"_+", "_", s)[:24].rstrip("_")
    return s or "segment"


def _norm(name: str) -> str:
    """Segment identity across years: 'Service' and 'Services' are the same line."""
    s = slug(name)
    return s[:-1] if s.endswith("s") and len(s) > 3 else s


def _peer_metrics(peers: list[dict], own: dict) -> list[Metric]:
    out = []
    for key, mid in (("pe_ttm", "pe_peer_median"), ("ev_sales_ttm", "ev_sales_peer_median"), ("ev_ebitda_ttm", "ev_ebitda_peer_median")):
        vals = [(p["peer"], p[key]) for p in peers if p.get(key) is not None and p[key] > 0]
        out.append(_m(mid, "current", median(v for _, v in vals) if vals else None, "peers:median", {"peers": [n for n, _ in vals]}, "no_peers"))
    med = {m.metric_id: m.value for m in out}
    for base, mid, ours in (("pe_peer_median", "pe_vs_peer_median", own.get("pe")), ("ev_sales_peer_median", "ev_sales_vs_peer_median", own.get("ev_sales"))):
        ok = med.get(base) and ours is not None and ours > 0
        reason = "no_peers" if not med.get(base) else "negative_earnings" if ours is not None and ours <= 0 else "no_coverage"
        out.append(_m(mid, "current", (ours / med[base]) if ok else None, "peers:median", {"own": ours, "peer_median": med.get(base)}, reason))
    return out


def _segment_metrics(segments: list[dict]) -> list[Metric]:
    """Ids carry the kind (seg_product_* / seg_geo_*) so a name shared by both kinds never collides; a segment keeps one
    identity across years (normalised name) and one display id (its latest name); two names that normalise alike within
    a year get a numeric suffix rather than overwriting each other."""
    out = []
    for kind, cap, prefix in (("product", MAX_PRODUCT, "seg_product_"), ("geographic", MAX_GEO, "seg_geo_")):
        rows = [r for r in segments if r["kind"] == kind and r.get("revenue") is not None]
        if not rows:
            continue
        by_year: dict[int, dict[str, tuple[str, float]]] = {}  # fy -> identity key -> (name, revenue)
        for fy in sorted({r["fiscal_year"] for r in rows}):
            year_rows = sorted((r for r in rows if r["fiscal_year"] == fy), key=lambda r: -r["revenue"])
            taken: dict[str, str] = {}
            for r in year_rows:
                key = _norm(r["name"])
                n = 2
                while key in taken and taken[key] != r["name"]:  # within-year collision: suffix, never drop
                    key = f"{_norm(r['name'])}_{n}"
                    n += 1
                taken[key] = r["name"]
                by_year.setdefault(fy, {})[key] = (r["name"], r["revenue"])
        latest = max(by_year)
        keep = list(by_year[latest])[:cap]
        display = {}
        for fy in sorted(by_year, reverse=True):  # the latest name an identity carries is its display id
            for key, (name, _) in by_year[fy].items():
                display.setdefault(key, slug(name))
        used = {}
        for key in keep:  # display slugs must be unique too
            d = display[key]
            n = 2
            while d in used.values():
                d = f"{display[key]}_{n}"
                n += 1
            used[key] = d
        formula = f"fmp:revenue-{kind}-segmentation"
        for fy in sorted(by_year):
            total = sum(v for _, v in by_year[fy].values())
            for key, (name, rev) in by_year[fy].items():
                if key not in used:
                    continue
                sid = prefix + used[key]
                prev = by_year.get(fy - 1, {}).get(key)
                out.append(_m(f"{sid}_revenue", f"FY{fy}", rev, formula, {"name": name, "kind": kind}, unit="USD"))
                out.append(_m(f"{sid}_share", f"FY{fy}", rev / total if total else None, formula, {"name": name, "kind": kind, "total": total}))
                out.append(_m(f"{sid}_yoy", f"FY{fy}", (rev / prev[1] - 1) if prev and prev[1] else None, formula, {"name": name, "kind": kind, "prior": prev[1] if prev else None}, "no_prior"))
    return out


def _dividend_metrics(dividends: list[dict], price: float | None, today: date) -> list[Metric]:
    rows = sorted((d for d in dividends if d.get("ex_date") and (d.get("adj_amount") is not None or d.get("amount") is not None)), key=lambda d: d["ex_date"])

    def amt(d):  # split-adjusted when FMP gives it, so a pre-split payment is not four times too big
        return d["adj_amount"] if d.get("adj_amount") is not None else d["amount"]
    ttm = [d for d in rows if d["ex_date"] > today - timedelta(days=365) and d["ex_date"] <= today]
    if not rows:
        dps = None
    else:
        dps = sum(amt(d) for d in ttm) if ttm else 0.0  # paid before but not in the last year: zero, which is the fact
    out = [_m("dividend_per_share_ttm", "current", dps, "fmp:dividends", {"payments": len(ttm)}, "no_dividend"),
           _m("dividend_yield", "current", (dps / price) if dps is not None and price else None, "fmp:dividends", {"dps_ttm": dps, "price": price}, "no_dividend" if dps is None else "no_coverage")]
    years: dict[int, float] = {}
    for d in rows:
        years[d["ex_date"].year] = years.get(d["ex_date"].year, 0.0) + amt(d)
    full = sorted(y for y in years if y < today.year)  # completed calendar years only
    growth, reason = None, "insufficient_history"
    if rows and (not full or full[-1] < today.year - 1):
        reason = "lapsed"  # no payment last year: a growth rate from the paying years would mislead
    elif len(full) >= 6 and years[full[-6]] > 0:
        growth = (years[full[-1]] / years[full[-6]]) ** (1 / 5) - 1
    elif not rows:
        reason = "no_dividend"
    out.append(_m("dividend_growth_5y", "current", growth, "fmp:dividends", {"from_year": full[-6] if len(full) >= 6 else None, "to_year": full[-1] if full else None}, reason))
    return out


def compute_fmp_metrics(peers: list[dict], segments: list[dict], dividends: list[dict], scores: dict | None, treasury: tuple | None, price: float | None, own: dict,
                        today: date | None = None) -> list[Metric]:
    today = today or date.today()
    out = _peer_metrics(peers, own) + _segment_metrics(segments) + _dividend_metrics(dividends, price, today)
    out.append(_m("altman_z", "current", (scores or {}).get("altman_z"), "fmp:financial-scores"))
    out.append(_m("piotroski", "current", (scores or {}).get("piotroski"), "fmp:financial-scores"))
    out.append(_m("risk_free_10y", "current", (treasury[1] / 100) if treasury else None, "fmp:treasury-rates", {"date": treasury[0].isoformat()} if treasury else {}))
    return out
