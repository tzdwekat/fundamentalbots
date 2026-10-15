"""Turn normalized XBRL facts into per-item period series (annual, quarterly, derived Q4).

Rules (spec, "Period selection from companyfacts"):
- Only 10-K/10-Q (and amendments) feed the series.
- Durations: annual 350-380 days, quarterly 80-100 days; anything else (YTD) is dropped.
- Per (start, end) the latest-filed value wins; restated if it moved >0.5% from the first filed value.
- Q4 = FY - (Q1+Q2+Q3), flagged derived.
- Fiscal year of a period end is resolved by date against the company's fiscal year end.
"""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

PERIODIC_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
TAG_MAP_PATH = Path(__file__).parent / "tag_map.yaml"
class InsufficientHistory(ValueError):
    pass


COLUMNS = ["period", "fy", "fq", "start", "end", "value", "accession", "tag", "restated", "derived"]


def load_tag_map() -> dict[str, list[str]]:
    return yaml.safe_load(TAG_MAP_PATH.read_text())


def _periodic(facts: pd.DataFrame) -> pd.DataFrame:
    df = facts[(facts.taxonomy == "us-gaap") & facts.form.isin(PERIODIC_FORMS)].copy()
    df["dur"] = (pd.to_datetime(df.end_date) - pd.to_datetime(df.start_date)).dt.days
    return df


def _is_annual(df):
    return (df.dur >= 350) & (df.dur <= 380)


def _is_quarter(df):
    return (df.dur >= 80) & (df.dur <= 100)


def choose_tag(df: pd.DataFrame, tags: list[str]) -> str | None:
    """The tag whose most recent annual (or instant) fact is latest; ties keep list order."""
    best = None
    for tag in tags:
        sub = df[df.tag == tag]
        if sub.empty:
            continue
        if sub.dur.isna().all():
            latest = sub.end_date.max()
        else:
            ann = sub[_is_annual(sub)]
            if ann.empty:
                continue
            latest = ann.end_date.max()
        if best is None or latest > best[0]:
            best = (latest, tag)
    return best[1] if best else None


def fiscal_year_end(facts: pd.DataFrame, tag_map: dict[str, list[str]]) -> tuple[int, int]:
    df = _periodic(facts)
    for tags in tag_map.values():
        tag = choose_tag(df, tags)
        if tag is None:
            continue
        ann = df[(df.tag == tag) & _is_annual(df)]
        if ann.empty:
            continue
        end = ann.end_date.max()
        return end.month, end.day
    raise InsufficientHistory("no annual 10-K facts in companyfacts (new registrant CIK, or not a 10-K filer)")


def _safe_date(y: int, m: int, d: int) -> date:
    while True:
        try:
            return date(y, m, d)
        except ValueError:
            d -= 1


def fy_of(end: date, fye: tuple[int, int]) -> int:
    return end.year if end <= _safe_date(end.year, *fye) + timedelta(days=10) else end.year + 1


def fq_of(end: date, fy: int, fye: tuple[int, int]) -> int:
    start = _safe_date(fy - 1, *fye) + timedelta(days=1)
    months = (end.year - start.year) * 12 + end.month - start.month + (end.day - start.day) / 30
    return round(months / 3)


def _dedupe(sub: pd.DataFrame) -> pd.DataFrame:
    """One row per (start, end): latest filed value, restated flag vs the first filed value."""
    sub = sub.sort_values(["filed", "accession"])
    key = ["start_date", "end_date"]
    first = sub.groupby(key, dropna=False).first()
    last = sub.groupby(key, dropna=False).last()
    out = last.reset_index()
    out["restated"] = ((last.value - first.value).abs() / first.value.abs().replace(0, float("nan")) > 0.005).fillna(False).values
    return out


def _build_item(df: pd.DataFrame, tag: str, fye: tuple[int, int]) -> pd.DataFrame:
    sub = _dedupe(df[df.tag == tag])
    rows = []
    ytd = {}  # (fy, fq) -> row for 6- and 9-month year-to-date facts
    instant = sub.start_date.isna().all()
    for r in sub.itertuples(index=False):
        end = r.end_date
        fy = fy_of(end, fye)
        fq = fq_of(end, fy, fye)
        if instant:
            if fq < 1 or fq > 4:
                continue
            rows.append((fy, fq, None, end, r.value, r.accession, r.restated, False))
            if fq == 4:
                rows.append((fy, 0, None, end, r.value, r.accession, r.restated, False))
        elif 350 <= r.dur <= 380:
            rows.append((fy, 0, r.start_date, end, r.value, r.accession, r.restated, False))
        elif 80 <= r.dur <= 100 and 1 <= fq <= 4:
            rows.append((fy, fq, r.start_date, end, r.value, r.accession, r.restated, False))
        elif (170 <= r.dur <= 190 and fq == 2) or (260 <= r.dur <= 280 and fq == 3):
            ytd[(fy, fq)] = (fy, fq, r.start_date, end, r.value, r.accession, r.restated)
    out = pd.DataFrame(rows, columns=["fy", "fq", "start", "end", "value", "accession", "restated", "derived"])
    out = out.drop_duplicates(["fy", "fq"], keep="last")
    if not instant:
        out = _derive_from_ytd(out, ytd)
        out = _derive_q4(out)
    out["tag"] = tag
    out["period"] = [f"FY{fy}" if fq == 0 else f"Q{fq}FY{fy}" for fy, fq in zip(out.fy, out.fq)]
    return out.sort_values(["end", "fq"]).reset_index(drop=True)[COLUMNS]


def _derive_from_ytd(out: pd.DataFrame, ytd: dict) -> pd.DataFrame:
    """Q2 = YTD6 - Q1; Q3 = YTD9 - YTD6 (or YTD9 - Q1 - Q2). Flow statements in 10-Qs are cumulative."""
    have = {(int(fy), int(fq)): row for fy, fq, row in zip(out.fy, out.fq, out.itertuples(index=False))}
    extra = []
    for (fy, fq), y in sorted(ytd.items()):
        if (fy, fq) in have:
            continue
        _, _, ystart, yend, yval, yacc, yrest = y
        if fq == 2 and (fy, 1) in have:
            q1 = have[(fy, 1)]
            prior, start, restated = q1.value, q1.end + timedelta(days=1), yrest or q1.restated
        elif fq == 3 and (fy, 2) in ytd:
            y6 = ytd[(fy, 2)]
            prior, start, restated = y6[4], y6[3] + timedelta(days=1), yrest or y6[6]
        elif fq == 3 and (fy, 1) in have and (fy, 2) in have:
            q1, q2 = have[(fy, 1)], have[(fy, 2)]
            prior, start, restated = q1.value + q2.value, q2.end + timedelta(days=1), yrest or q1.restated or q2.restated
        else:
            continue
        row = (fy, fq, start, yend, yval - prior, yacc, restated, True)
        extra.append(row)
        have[(fy, fq)] = pd.Series(dict(zip(out.columns, row)))
    if extra:
        out = pd.concat([out, pd.DataFrame(extra, columns=out.columns)], ignore_index=True)
    return out


def _derive_q4(out: pd.DataFrame) -> pd.DataFrame:
    extra = []
    for fy, grp in out.groupby("fy"):
        by_q = {int(q): row for q, row in zip(grp.fq, grp.itertuples(index=False))}
        if 0 in by_q and 4 not in by_q and all(q in by_q for q in (1, 2, 3)):
            a = by_q[0]
            v = a.value - sum(by_q[q].value for q in (1, 2, 3))
            restated = any(by_q[q].restated for q in (0, 1, 2, 3))
            extra.append((fy, 4, by_q[3].end + timedelta(days=1), a.end, v, a.accession, restated, True))
    if extra:
        out = pd.concat([out, pd.DataFrame(extra, columns=out.columns)], ignore_index=True)
    return out


def build_series(facts: pd.DataFrame, tag_map: dict[str, list[str]]) -> dict[str, pd.DataFrame]:
    df = _periodic(facts)
    fye = fiscal_year_end(facts, tag_map)
    series = {}
    for item, tags in tag_map.items():
        tag = choose_tag(df, tags)
        if tag is None:
            continue
        s = _build_item(df, tag, fye)
        if not s.empty:
            series[item] = s
    return series


def ttm(series: pd.DataFrame, as_of: date) -> float | None:
    """Sum of the last four quarters ending on or before as_of; None unless they span one contiguous year."""
    q = series[(series.fq >= 1) & (series.end <= as_of)].sort_values("end").tail(4)
    if len(q) < 4 or q.start.isna().any():
        return None
    span = (q.end.iloc[-1] - q.start.iloc[0]).days
    if not 350 <= span <= 380:
        return None
    return float(q.value.sum())
