"""Rules-only red flags. Numeric rules here; text and filing-list rules added by edgar_filings ingest."""
from dataclasses import dataclass, field, asdict

from fundabot.analytics.metrics import Metric


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
