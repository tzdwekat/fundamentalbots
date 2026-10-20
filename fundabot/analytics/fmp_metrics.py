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
        out.append(_m(mid, "current", (ours / med[base]) if ok else None, "peers:median", {"own": ours, "peer_median": med.get(base)}, "no_peers" if not med.get(base) else "no_coverage"))
    return out


def _segment_metrics(segments: list[dict]) -> list[Metric]:
    out = []
    for kind, cap in (("product", MAX_PRODUCT), ("geographic", MAX_GEO)):
        rows = [r for r in segments if r["kind"] == kind and r.get("revenue") is not None]
        if not rows:
            continue
        latest = max(r["fiscal_year"] for r in rows)
        keep = {_norm(r["name"]) for r in sorted((r for r in rows if r["fiscal_year"] == latest), key=lambda r: -r["revenue"])[:cap]}
        by_year: dict[int, dict[str, tuple[str, float]]] = {}
        for r in rows:
            by_year.setdefault(r["fiscal_year"], {})[_norm(r["name"])] = (r["name"], r["revenue"])
        formula = f"fmp:revenue-{kind}-segmentation"
        for fy in sorted(by_year):
            total = sum(v for _, v in by_year[fy].values())
            for key, (name, rev) in by_year[fy].items():
                if key not in keep:
                    continue
                sid = slug(name)
                prev = by_year.get(fy - 1, {}).get(key)
                out.append(_m(f"seg_{sid}_revenue", f"FY{fy}", rev, formula, {"name": name}, unit="USD"))
                out.append(_m(f"seg_{sid}_share", f"FY{fy}", rev / total if total else None, formula, {"name": name, "total": total}))
                out.append(_m(f"seg_{sid}_yoy", f"FY{fy}", (rev / prev[1] - 1) if prev and prev[1] else None, formula, {"name": name, "prior": prev[1] if prev else None}, "no_prior"))
    return out


def _dividend_metrics(dividends: list[dict], price: float | None, today: date) -> list[Metric]:
    rows = sorted((d for d in dividends if d.get("ex_date") and d.get("amount") is not None), key=lambda d: d["ex_date"])
    ttm = [d for d in rows if d["ex_date"] > today - timedelta(days=365) and d["ex_date"] <= today]
    dps = sum(d["amount"] for d in ttm) if ttm else None
    out = [_m("dividend_per_share_ttm", "current", dps, "fmp:dividends", {"payments": len(ttm)}),
           _m("dividend_yield", "current", (dps / price) if dps is not None and price else None, "fmp:dividends", {"dps_ttm": dps, "price": price})]
    years: dict[int, float] = {}
    for d in rows:
        years[d["ex_date"].year] = years.get(d["ex_date"].year, 0.0) + (d.get("adj_amount") if d.get("adj_amount") is not None else d["amount"])
    full = sorted(y for y in years if y < today.year)  # completed calendar years only
    growth = None
    if len(full) >= 6 and years[full[-6]] > 0:
        growth = (years[full[-1]] / years[full[-6]]) ** (1 / 5) - 1
    out.append(_m("dividend_growth_5y", "current", growth, "fmp:dividends", {"from_year": full[-6] if len(full) >= 6 else None, "to_year": full[-1] if full else None}, "insufficient_history"))
    return out


def compute_fmp_metrics(peers: list[dict], segments: list[dict], dividends: list[dict], scores: dict | None, treasury: tuple | None, price: float | None, own: dict,
                        today: date | None = None) -> list[Metric]:
    today = today or date.today()
    out = _peer_metrics(peers, own) + _segment_metrics(segments) + _dividend_metrics(dividends, price, today)
    out.append(_m("altman_z", "current", (scores or {}).get("altman_z"), "fmp:financial-scores"))
    out.append(_m("piotroski", "current", (scores or {}).get("piotroski"), "fmp:financial-scores"))
    out.append(_m("risk_free_10y", "current", (treasury[1] / 100) if treasury else None, "fmp:treasury-rates", {"date": treasury[0].isoformat()} if treasury else {}))
    return out
