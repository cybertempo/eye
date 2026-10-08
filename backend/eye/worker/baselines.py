"""Coverage-weighted baselines over hourly rollups (Package 5, research slice).

Pure functions: no database, no clock, no randomness, no model. The input is
an hourly series read from recorded rollup manifests (``backtest.py``); the
output is a list of deterministic facts per target bin. Nothing here writes
prose: every result is numbers, a verdict and, when it abstains, a reason code
from a fixed list.

* **Exposure.** An hour counts only when its metric row is exact
  (``qualified``) and its coverage rollup says the whole hour was qualified.
  Its exposure is then 3600 s and its value is exact. A partial hour (a lower
  bound), an unknown or failed hour, an hour with no manifest and an hour whose
  coverage row disagrees all have exposure 0 and contribute nothing: never a
  zero count.
* **Baseline.** For a target bin (``bin_hours`` long, aligned to the UTC day)
  the history is the same bin on each of the previous ``history_days`` days.
  The baseline rate is the total exact count over the total exposure, so a bin
  that was half covered weighs half as much. The expected value of the target
  is that rate times the target's own exposure.
* **Abstention.** A target whose exposure is below ``min_target_coverage`` of
  the bin abstains (``target_not_covered``); a history with less exposure than
  ``min_history_bins`` whole bins abstains (``history_insufficient``). An
  abstained result has no observed or expected value.
* **Detection** is one-sided (more than expected). The variance is the
  Poisson variance of the target plus that of the estimated rate, scaled by
  the history's over-dispersion (Pearson, at least 1), with the expected
  value floored at 1. A bin is ``detected`` only when the excess is at least
  ``min_excess`` and at least ``threshold_sigma`` standard deviations; both
  comparisons are exact (fractions), so a replay cannot round differently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction

ALGORITHM_VERSION = "baseline-backtest/1"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
HOUR_S = 3600
BIN_HOURS = (1, 2, 3, 4, 6, 8, 12, 24)
EXACT = "qualified"
UNKNOWN_STATES = ("unknown", "failed")
REASON_CODES = ("target_not_covered", "history_insufficient")
VERDICTS = ("detected", "not_detected", "abstained")
PLACES = Decimal("0.000001")


class BaselineRefused(ValueError):
    """The request is outside the configured bounds or malformed."""


@dataclass(frozen=True)
class Hour:
    """One hour of a rollup series, as recorded in its manifests.

    ``state`` is the metric row's state, or ``missing`` when no manifest or
    row exists for the hour. ``qualified_s`` comes from the coverage rollup of
    the same partition hour, or is None when that is missing.
    """

    start: datetime
    state: str
    value: int | None
    qualified_s: Fraction | None

    @property
    def exposure_s(self) -> int:
        if self.state == EXACT and self.value is not None and self.qualified_s == HOUR_S:
            return HOUR_S
        return 0


@dataclass(frozen=True)
class Params:
    bin_hours: int = 1
    history_days: int = 14
    min_history_bins: int = 3
    threshold_sigma: int = 5
    min_excess: int = 4
    min_target_coverage_pct: int = 100

    def __post_init__(self) -> None:
        if self.bin_hours not in BIN_HOURS:
            raise BaselineRefused(f"bin_hours must be one of {BIN_HOURS}")
        for name, low, high in (
            ("history_days", 1, 60),
            ("min_history_bins", 1, 60),
            ("threshold_sigma", 1, 20),
            ("min_excess", 1, 1_000_000),
            ("min_target_coverage_pct", 1, 100),
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise BaselineRefused(f"{name} must be an integer from {low} to {high}")
        if self.min_history_bins > self.history_days:
            raise BaselineRefused("min_history_bins cannot exceed history_days")

    @property
    def bin_s(self) -> int:
        return self.bin_hours * HOUR_S

    def as_dict(self) -> dict:
        return {
            "bin_hours": self.bin_hours,
            "history_days": self.history_days,
            "min_history_bins": self.min_history_bins,
            "threshold_sigma": self.threshold_sigma,
            "min_excess": self.min_excess,
            "min_target_coverage_pct": self.min_target_coverage_pct,
        }


@dataclass(frozen=True)
class BinFacts:
    """What one bin's hours support: exact counts over qualified exposure only."""

    count: int
    exposure_s: int
    hours_exact: int
    hours_partial: int
    hours_unknown: int
    hours_missing: int


@dataclass(frozen=True)
class Result:
    """One target bin's deterministic outcome. No prose: codes and numbers only."""

    day: date
    bin_index: int
    bin_start: datetime
    bin_end: datetime
    verdict: str
    reason_code: str | None
    observed: int | None
    exposure_s: int
    expected: Fraction | None
    score: Decimal | None
    detail: dict

    @property
    def row_key(self) -> str:
        return f"day:{self.day.isoformat()}:bin:{self.bin_index:02d}"


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def bin_facts(series: dict[datetime, Hour], start: datetime, hours: int) -> BinFacts:
    count = exposure = exact = partial = unknown = missing = 0
    for i in range(hours):
        hour = series.get(start + i * HOUR)
        if hour is None or hour.state == "missing":
            missing += 1
        elif hour.exposure_s:
            exposure += hour.exposure_s
            count += hour.value or 0
            exact += 1
        elif hour.state == "partial":
            partial += 1
        elif hour.state in UNKNOWN_STATES:
            unknown += 1
        else:
            # Exact metric but the coverage rollup disagrees or is missing:
            # the hour cannot be trusted as exact, so it is treated as missing.
            missing += 1
    return BinFacts(count, exposure, exact, partial, unknown, missing)


def fixed(value: Fraction | Decimal | int) -> Decimal:
    """Canonical decimal with six places (half-even), for storage and checksums."""
    if isinstance(value, Fraction):
        with localcontext() as ctx:
            ctx.prec = 40
            value = Decimal(value.numerator) / Decimal(value.denominator)
    return Decimal(value).quantize(PLACES, rounding=ROUND_HALF_EVEN)


def _score(excess: Fraction, variance: Fraction) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 40
        root = (Decimal(variance.numerator) / Decimal(variance.denominator)).sqrt()
        return fixed(Decimal(excess.numerator) / Decimal(excess.denominator) / root)


def evaluate_bin(series: dict[datetime, Hour], day: date, bin_index: int, params: Params) -> Result:
    """The baseline, expectation and verdict of one target bin. History is strictly earlier."""
    start = day_start(day) + bin_index * params.bin_hours * HOUR
    end = start + params.bin_hours * HOUR
    target = bin_facts(series, start, params.bin_hours)
    history = [
        bin_facts(series, start - k * DAY, params.bin_hours)
        for k in range(1, params.history_days + 1)
    ]
    used = [h for h in history if h.exposure_s > 0]
    hist_count = sum(h.count for h in used)
    hist_exposure = sum(h.exposure_s for h in used)
    detail = {
        "hours_exact": target.hours_exact,
        "hours_partial": target.hours_partial,
        "hours_unknown": target.hours_unknown,
        "hours_missing": target.hours_missing,
        "history_bins_used": len(used),
        "history_bins_abstained": len(history) - len(used),
        "history_count": hist_count,
        "history_exposure_s": hist_exposure,
    }

    def abstain(code: str) -> Result:
        return Result(
            day,
            bin_index,
            start,
            end,
            "abstained",
            code,
            None,
            target.exposure_s,
            None,
            None,
            detail,
        )

    # Coverage gate on the target: missing, unknown or partial hours are not zero.
    if target.exposure_s * 100 < params.min_target_coverage_pct * params.bin_s:
        return abstain("target_not_covered")
    if hist_exposure < params.min_history_bins * params.bin_s:
        return abstain("history_insufficient")

    rate = Fraction(hist_count, hist_exposure)  # per second of exact exposure
    expected = rate * target.exposure_s
    # Over-dispersion of the history bins around the pooled rate (Pearson).
    chi2 = sum(
        (Fraction(h.count) - rate * h.exposure_s) ** 2 / max(rate * h.exposure_s, Fraction(1))
        for h in used
    )
    phi = max(Fraction(1), chi2 / (len(used) - 1)) if len(used) > 1 else Fraction(1)
    floor = max(expected, Fraction(1))
    variance = phi * (floor + floor * target.exposure_s / hist_exposure)
    excess = Fraction(target.count) - expected
    detected = excess >= params.min_excess and excess * excess >= (
        params.threshold_sigma**2 * variance
    )
    detail["dispersion_micro"] = int(fixed(phi) * 1_000_000)
    return Result(
        day,
        bin_index,
        start,
        end,
        "detected" if detected else "not_detected",
        None,
        target.count,
        target.exposure_s,
        expected,
        _score(excess, variance),
        detail,
    )


def backtest(
    series: dict[datetime, Hour], first_day: date, last_day: date, params: Params
) -> list[Result]:
    """Walk forward over every bin of every target day; each uses only earlier days."""
    if last_day < first_day:
        raise BaselineRefused("the backtest range ends before it starts")
    bins = 24 // params.bin_hours
    out = []
    day = first_day
    while day <= last_day:
        out += [evaluate_bin(series, day, b, params) for b in range(bins)]
        day += DAY
    return out


def canonical(result: Result) -> list:
    """The stored form of one result, used for checksums and replay comparison."""
    return [
        result.row_key,
        result.bin_start.isoformat(),
        result.bin_end.isoformat(),
        result.verdict,
        result.reason_code,
        result.observed,
        result.exposure_s,
        None if result.expected is None else str(fixed(result.expected)),
        None if result.score is None else str(result.score),
        {k: result.detail[k] for k in sorted(result.detail)},
    ]


def summary(results: list[Result]) -> dict:
    return {v: sum(1 for r in results if r.verdict == v) for v in VERDICTS}
