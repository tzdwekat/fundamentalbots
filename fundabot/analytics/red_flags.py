"""Rules-only red flags. Numeric rules here; text and filing-list rules added by edgar_filings ingest."""
import re
from dataclasses import dataclass, field, asdict
from datetime import timedelta

import pandas as pd

from fundabot.analytics.metrics import Metric

GOING_CONCERN = re.compile(r"substantial doubt.{0,300}going concern|going concern.{0,300}substantial doubt", re.I | re.S)
NEGATED = re.compile(r"\b(no|not|does not|do not|did not|alleviated?)\b", re.I)  # ASU 2014-15 boilerplate in healthy filers
GOING_CONCERN_ITEMS = ("item7", "item8", "item9a")  # not 1A: risk factors carry boilerplate


@dataclass
class RedFlag:
    flag_id: str
    severity: str  # high | medium
    evidence: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self):
        return asdict(self)


def _fy_rows(metrics: list[Metric], metric_id: str) -> list[Metric]:
    rows = [m for m in metrics if m.metric_id == metric_id and m.period.startswith("FY") and m.value is not None]
    return sorted(rows, key=lambda m: m.period)


def numeric_flags(metrics: list[Metric]) -> list[RedFlag]:
    flags = []
    acc = _fy_rows(metrics, "accruals_ratio")
    if acc and acc[-1].value > 0.10:
        flags.append(RedFlag("accruals_high", "high", [f"metric:accruals_ratio@{acc[-1].period}"],
                             f"accruals ratio {acc[-1].value:.1%} in {acc[-1].period} (threshold 10%)"))
    fcf = _fy_rows(metrics, "fcf_to_net_income")[-5:]
    below = [m for m in fcf if m.value < 1]
    if len(below) >= 3:
        flags.append(RedFlag("fcf_below_ni", "medium", [f"metric:fcf_to_net_income@{m.period}" for m in below],
                             f"FCF below net income in {len(below)} of the last {len(fcf)} fiscal years"))
    gw = _fy_rows(metrics, "goodwill_to_assets")
    if gw and gw[-1].value > 0.5:
        flags.append(RedFlag("goodwill_heavy", "medium", [f"metric:goodwill_to_assets@{gw[-1].period}"],
                             f"goodwill is {gw[-1].value:.0%} of total assets in {gw[-1].period}"))
    return flags


def text_flags(accession: str, sections: dict[str, str], filings: pd.DataFrame) -> list[RedFlag]:
    """Going-concern language in the latest 10-K's MD&A/financials/controls; NT 10-K/10-Q filings in the last 2 years."""
    flags = []
    for item in GOING_CONCERN_ITEMS:
        for m in GOING_CONCERN.finditer(sections.get(item, "")):
            text = sections[item]
            start, end = text.rfind(".", 0, m.start()) + 1, text.find(".", m.end())
            sentence = " ".join(text[start:end if end > 0 else None].split())
            if NEGATED.search(sentence):
                continue
            flags.append(RedFlag("going_concern", "high", [f"chunk:{accession}#{item}-0"], f"{item} of {accession}: \"{sentence[:300]}\""))
            break
        if flags:
            break
    if len(filings):
        cutoff = filings.filed.max() - timedelta(days=730)
        late = filings[filings.form.str.startswith("NT 10-") & (filings.filed >= cutoff)]
        if len(late):
            flags.append(RedFlag("late_filing", "medium", [f"filing:{a}" for a in late.accession],
                                 f"{len(late)} late-filing notice(s) (NT 10-K/10-Q) in the last 2 years"))
    return flags
