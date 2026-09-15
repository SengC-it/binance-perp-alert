"""Pre-registered M1 gate calculations.

This module contains pure, deterministic evaluators only.  It never downloads
data and it has no manual override path.  Formal M1-B code must pass its
frozen metrics into these functions after the external/discovery split is
already established.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable, Mapping, Sequence

from .m1_protocol import InclusiveWindow, load_protocol, protocol_windows, verify_protocol_hash


class GateError(ValueError):
    """Invalid or incomplete input for a frozen gate."""


class ManualOverrideError(GateError):
    """M1 decisions cannot be manually overridden."""


@dataclass(frozen=True)
class GateEvaluation:
    decision: str
    gates: Mapping[str, bool]
    failed_gates: tuple[str, ...]
    missing_metrics: tuple[str, ...] = ()


def _group(metrics: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = metrics.get(name)
    return value if isinstance(value, Mapping) else {}


def _value(metrics: Mapping[str, Any], group: str, key: str, *aliases: str) -> Any:
    grouped = _group(metrics, group)
    for candidate in (key, *aliases):
        if candidate in grouped:
            return grouped[candidate]
        if candidate in metrics:
            return metrics[candidate]
    return None


def _bool(value: Any) -> bool:
    return value is True


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def evaluate_frozen_gates(
    metrics: Mapping[str, Any],
    *,
    protocol: Mapping[str, Any] | None = None,
    manual_override: Any = None,
) -> GateEvaluation:
    """Evaluate G0-G12 with fixed thresholds and fail closed on missing data."""
    if manual_override is not None or metrics.get("manual_override") is not None:
        raise ManualOverrideError("M1 gate 不允许人工 override")
    protocol = protocol or load_protocol()
    external, discovery = protocol_windows(protocol)
    if external.name != "EXTERNAL_VALIDATION" or discovery.name != "DISCOVERY_REFERENCE":
        raise GateError("external/discovery window 名称未按 protocol 冻结")

    missing: list[str] = []

    def required_bool(group: str, key: str) -> bool:
        value = _value(metrics, group, key)
        if value is None:
            missing.append(f"{group}.{key}")
        return _bool(value)

    def required_number(group: str, key: str, predicate: Callable[[float], bool]) -> bool:
        value = _number(_value(metrics, group, key))
        if value is None:
            missing.append(f"{group}.{key}")
            return False
        return predicate(value)

    gates: dict[str, bool] = {
        "G0_data_integrity": all(
            required_bool("data_integrity", key)
            for key in (
                "point_in_time_universe",
                "delisted_included",
                "no_known_lookahead",
                "no_unexplained_eligible_gap",
                "strategy_hash_unchanged",
                "protocol_hash_unchanged",
            )
        ),
        "G1_external_return": required_number("external", "total_return_pct", lambda value: value > 0),
        "G2_cost_2x": required_number("cost_2x", "total_return_pct", lambda value: value > 0),
        "G3_weekly_sharpe": required_number("external", "weekly_sharpe", lambda value: value > 1.0),
        "G4_drawdown": required_number("external", "max_drawdown_pct", lambda value: value < 20.0),
        "G5_profit_factor": required_number("external", "profit_factor_weekly", lambda value: value > 1.20),
        "G6_block_bootstrap": (
            required_number("bootstrap", "mean_weekly_return_ci95_lower", lambda value: value > 0)
            and required_number("bootstrap", "probability_mean_return_gt_zero", lambda value: value >= 0.95)
        ),
        "G7_best_5pct_removed": required_number("best_5pct", "compound_return_pct", lambda value: value > 0),
        "G8_leave_one_out": (
            _number(_value(metrics, "leave_one_out", "positive_runs")) is not None
            and _number(_value(metrics, "leave_one_out", "total_runs")) is not None
            and _number(_value(metrics, "leave_one_out", "total_runs")) > 0
            and _number(_value(metrics, "leave_one_out", "positive_runs"))
            == _number(_value(metrics, "leave_one_out", "total_runs"))
        ),
        "G9_symbol_concentration": required_number(
            "concentration", "top2_positive_pnl_share_pct", lambda value: value <= 40.0
        ),
        "G10_multi_year_breadth": required_number(
            "yearly", "positive_full_calendar_years", lambda value: value >= 3
        ),
        "G11_single_year_catastrophe": required_number(
            "yearly", "worst_full_calendar_year_return_pct", lambda value: value > -15.0
        ),
        "G12_bull_survival": (
            required_number("bull_2020_2021", "total_return_pct", lambda value: value > -10.0)
            and required_number("bull_2020_2021", "max_drawdown_pct", lambda value: value < 20.0)
        ),
    }

    # The two G8 fields need explicit missing markers because a missing value
    # otherwise looks identical to an ordinary false result.
    for key in ("positive_runs", "total_runs"):
        if _number(_value(metrics, "leave_one_out", key)) is None:
            missing.append(f"leave_one_out.{key}")
    if metrics.get("formal_run") is not True:
        missing.append("formal_run")
    failed = tuple(name for name, passed in gates.items() if not passed)
    return GateEvaluation(
        decision="PASS" if not failed and not missing else "FAIL",
        gates=gates,
        failed_gates=failed,
        missing_metrics=tuple(dict.fromkeys(missing)),
    )


def split_windowed_rows(
    rows: Iterable[Mapping[str, Any]],
    external: InclusiveWindow,
    discovery: InclusiveWindow,
    *,
    date_key: str = "day",
) -> dict[str, list[Mapping[str, Any]]]:
    """Strictly split rows; discovery rows can never enter external output."""
    output = {"EXTERNAL_VALIDATION": [], "DISCOVERY_REFERENCE": []}
    for row in rows:
        try:
            row_day = date.fromisoformat(str(row[date_key])[:10])
        except (KeyError, TypeError, ValueError) as exc:
            raise GateError("window row 缺少合法 day") from exc
        if external.contains(row_day):
            output["EXTERNAL_VALIDATION"].append(row)
        elif discovery.contains(row_day):
            output["DISCOVERY_REFERENCE"].append(row)
        else:
            raise GateError(f"row {row_day} 不属于 external 或 discovery window")
    return output


def compound_return(returns: Sequence[float]) -> float:
    equity = 1.0
    for value in returns:
        number = _number(value)
        if number is None or number <= -1.0:
            raise GateError("return 必须是有限且大于 -100% 的小数")
        equity *= 1.0 + number
    return equity - 1.0


def weekly_sharpe(returns: Sequence[float], periods_per_year: float = 52.0) -> float:
    values = [float(value) for value in returns]
    if len(values) < 2:
        return 0.0
    deviation = statistics.pstdev(values)
    return statistics.fmean(values) / deviation * math.sqrt(periods_per_year) if deviation else 0.0


def weekly_profit_factor(returns: Sequence[float]) -> float:
    positive = sum(value for value in returns if value > 0)
    negative = abs(sum(value for value in returns if value < 0))
    return positive / negative if negative else (float("inf") if positive else 0.0)


def max_drawdown_pct(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = equity
    worst = 0.0
    for value in returns:
        equity *= 1.0 + float(value)
        peak = max(peak, equity)
        if peak:
            worst = min(worst, (equity / peak - 1.0) * 100.0)
    return abs(worst)


def remove_best_5pct(weekly_returns: Sequence[float]) -> dict[str, Any]:
    """Remove exactly the pre-registered highest 5% (ceil, minimum one)."""
    values = [float(value) for value in weekly_returns]
    if not values:
        raise GateError("best-week removal 需要 weekly returns")
    remove_count = max(1, math.ceil(len(values) * 0.05))
    removed_indices = set(
        sorted(range(len(values)), key=lambda index: (values[index], index), reverse=True)[:remove_count]
    )
    remaining = [value for index, value in enumerate(values) if index not in removed_indices]
    return {
        "removed_count": remove_count,
        "removed_indices": tuple(sorted(removed_indices)),
        "compound_return": compound_return(remaining),
        "compound_return_pct": compound_return(remaining) * 100.0,
    }


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise GateError("quantile 需要非空序列")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def block_bootstrap(
    weekly_returns: Sequence[float],
    *,
    block_length: int = 4,
    rounds: int = 10_000,
    confidence: float = 0.95,
    seed: int = 20260915,
) -> dict[str, Any]:
    """Deterministic fixed-block bootstrap for the pre-registered gate."""
    values = [float(value) for value in weekly_returns]
    if not values or block_length <= 0 or rounds <= 0 or not 0 < confidence < 1:
        raise GateError("bootstrap 参数或 returns 非法")
    rng = random.Random(seed)
    means: list[float] = []
    sharpes: list[float] = []
    block_count = math.ceil(len(values) / block_length)
    max_start = max(0, len(values) - block_length)
    for _ in range(rounds):
        sample: list[float] = []
        for _ in range(block_count):
            start = rng.randrange(max_start + 1)
            sample.extend(values[start:start + block_length])
        sample = sample[:len(values)]
        means.append(statistics.fmean(sample))
        sharpes.append(weekly_sharpe(sample))
    alpha = (1.0 - confidence) / 2.0
    return {
        "block_length_weeks": block_length,
        "rounds": rounds,
        "confidence": confidence,
        "seed": seed,
        "mean_weekly_return_ci95": (_quantile(means, alpha), _quantile(means, 1.0 - alpha)),
        "mean_weekly_return_ci95_lower": _quantile(means, alpha),
        "weekly_sharpe_ci95": (_quantile(sharpes, alpha), _quantile(sharpes, 1.0 - alpha)),
        "probability_mean_return_gt_zero": sum(value > 0 for value in means) / rounds,
    }


def leave_one_out(
    symbols: Iterable[str],
    rerun: Callable[[frozenset[str]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Re-run ranking/equity once per removed symbol; never subtract final PnL."""
    universe = tuple(sorted({str(symbol) for symbol in symbols}))
    if not universe:
        raise GateError("LOO 需要非空 symbol universe")
    runs: list[dict[str, Any]] = []
    for removed in universe:
        result = rerun(frozenset(symbol for symbol in universe if symbol != removed))
        value = _number(result.get("total_return_pct"))
        if value is None:
            raise GateError(f"LOO {removed} 缺少 total_return_pct")
        runs.append({"removed_symbol": removed, "total_return_pct": value})
    returns = [run["total_return_pct"] for run in runs]
    worst = min(runs, key=lambda run: (run["total_return_pct"], run["removed_symbol"]))
    return {
        "loo_total": len(runs),
        "loo_positive": sum(value > 0 for value in returns),
        "loo_negative": sum(value <= 0 for value in returns),
        "loo_min_return": min(returns),
        "loo_median_return": statistics.median(returns),
        "loo_worst_removed_symbol": worst["removed_symbol"],
        "runs": tuple(runs),
    }


def symbol_concentration(pnl_by_symbol: Mapping[str, float]) -> dict[str, Any]:
    positive = {str(symbol): max(0.0, float(value)) for symbol, value in pnl_by_symbol.items()}
    total = sum(positive.values())
    ranked = sorted(positive.items(), key=lambda pair: (-pair[1], pair[0]))
    share = lambda count: (sum(value for _, value in ranked[:count]) / total * 100.0) if total else 0.0
    return {
        "top_1_profit_symbol": ranked[0][0] if ranked else None,
        "top_1_share_of_positive_pnl_pct": share(1),
        "top2_positive_pnl_share_pct": share(2),
        "top_5_share_of_positive_pnl_pct": share(5),
        "ranked": tuple(ranked),
    }
