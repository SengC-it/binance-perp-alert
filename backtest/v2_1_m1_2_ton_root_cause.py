"""XS-LOWVOL V2.1-M1.2 TONUSDT lifecycle/funding root-cause audit.

This audit is deliberately narrower than a validation run.  It reads the
already frozen V1 normalized cache, reconstructs only the V1 Control holding
interval needed for TONUSDT, and records independent Binance read-only
provenance around the announced contract settlement.  It does not change the
strategy, protocol, anchor, accounting, validator, or any historical result.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

import requests

from src.xs_lowvol_spec import (
    CONTROL_SPEC_HASH,
    SHADOW_SPEC_HASH,
    resolve_rules,
    strategy_spec_hash as v1_strategy_spec_hash,
)
from src.xs_lowvol_v2_1_anchor import (
    APPROVED_V1_CONTROL_SHA256,
    APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_1_PROTOCOL_SHA256,
    APPROVED_V2_1_SPEC_SHA256,
    V21_FORWARD_ANCHOR_STATUS,
    validate_v2_1_forward_anchor,
    verify_v2_1_forward_anchor_hash,
)
from src.xs_lowvol_v2_1_spec import verify_v2_1_spec_hash

from .m1_b import build_stateful_schedule, simulate_frozen_portfolio
from .m1_protocol import M1_APPROVED_PROTOCOL_SHA256, verify_protocol_hash
from .v2_1_m1_engineering import dataset_metadata, load_verified_dataset
from .v2_1_protocol import verify_v2_1_protocol_hash
from .xs_data_quality import HoldingInterval, validate_funding_coverage_for_holds


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_ID = "XS-LOWVOL-V2.1-M1.2-TON-LIFECYCLE-1"
BASE_COMMIT = "6b55cccf9e926317d5415365b7fc656cd56b4157"
APPROVAL = "START V2.1-M1.2"
SYMBOL = "TONUSDT"
OUTPUT_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_2"
REPORT_PATH = OUTPUT_DIR / "V2_1_M1_2_TON_ROOT_CAUSE.json"
MARKDOWN_PATH = OUTPUT_DIR / "V2_1_M1_2_TON_ROOT_CAUSE.md"
PROVENANCE_PATH = OUTPUT_DIR / "TON_OFFICIAL_PROVENANCE_MANIFEST.json"
RAW_DIR = PROJECT_ROOT / "data" / "v2_1_m1_2_ton_root_cause"

BINANCE_FAPI_BASE = "https://fapi.binance.com"
ANNOUNCEMENT_PAGE_URL = (
    "https://www.binance.com/en/support/announcement/"
    "detail/fe307fba935b44698fde4db01e84a7eb"
)
ANNOUNCEMENT_API_URL = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
)
ANNOUNCEMENT_CODE = "fe307fba935b44698fde4db01e84a7eb"
ANNOUNCEMENT_TITLE = "Binance Will Support the Toncoin (TON) Rebranding to Gram (GRAM)"
FUNDING_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/fundingRate"
KLINE_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/klines"
MARK_KLINE_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/markPriceKlines"

UTC = timezone.utc
FUNDING_START_MS = 1781913600000  # 2026-06-20T00:00:00Z
FUNDING_END_MS = 1782259200000  # 2026-06-24T00:00:00Z
MARKET_START_MS = 1782198000000  # 2026-06-23T07:00:00Z
MARKET_END_MS = 1782208800000  # 2026-06-23T10:00:00Z
DAILY_START_MS = 1782086400000  # 2026-06-22T00:00:00Z
DAILY_END_MS = 1782259200000  # 2026-06-24T00:00:00Z
CONTRACT_SETTLEMENT_MS = 1782205200000  # 2026-06-23T09:00:00Z
NEW_ORDER_RESTRICTION_MS = 1782203400000  # 2026-06-23T08:30:00Z
REQUIRED_WEEK_START = date(2026, 6, 22)
REQUIRED_WEEK_END = date(2026, 6, 28)

_FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {
        "return",
        "cagr",
        "sharpe",
        "sortino",
        "max_drawdown",
        "profit_factor",
        "annual_return",
        "monthly_return",
        "bootstrap_performance",
        "best_week_removal_performance",
        "best_week_removed_return",
        "historical_profitability",
        "pnl",
        "performance",
    }
)


class M12AuditError(RuntimeError):
    """A fail-closed M1.2 audit error."""


@dataclass(frozen=True)
class ResponseRecord:
    """Metadata for one immutable GET response saved in the ignored cache."""

    name: str
    endpoint: str
    requested_url: str
    fetched_at_utc: str
    status_code: int
    content_sha256: str
    byte_count: int
    local_path: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        return sorted(_canonical(item) for item in value)
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_iso(timestamp_ms: int) -> str:
    text = datetime.fromtimestamp(timestamp_ms / 1000.0, UTC).isoformat(
        timespec="milliseconds"
    )
    text = text.replace("+00:00", "Z")
    return text.replace(".000Z", "Z")


def _utc_day_start_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def _schema_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _artifact_key_violations(value: Any, path: str = "") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            key_path = f"{path}.{key_text}" if path else key_text
            if _schema_key(key_text) in _FORBIDDEN_ARTIFACT_KEYS:
                violations.append(key_path)
            violations.extend(_artifact_key_violations(item, key_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            violations.extend(_artifact_key_violations(item, f"{path}[{index}]"))
    return violations


def assert_no_historical_result_fields(value: Mapping[str, Any]) -> None:
    violations = _artifact_key_violations(value)
    if violations:
        raise M12AuditError(
            "M1.2 artifact contains forbidden historical result fields: "
            + ", ".join(violations[:10])
        )


def _current_code_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise M12AuditError("cannot determine current code commit") from exc
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise M12AuditError("current code commit is not a full SHA-1")
    return commit


def _git_status_short() -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise M12AuditError("cannot inspect git status") from exc
    return result.stdout.strip()


def _base_is_ancestor(code_commit: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", BASE_COMMIT, code_commit],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise M12AuditError("cannot verify the approved M1.1 result base") from exc
    if result.returncode not in (0, 1):
        raise M12AuditError("git could not verify the approved M1.1 result base")
    return result.returncode == 0


def verify_frozen_identity() -> dict[str, str]:
    """Verify every immutable strategy, protocol, and anchor identity."""
    try:
        control = v1_strategy_spec_hash()
        shadow = resolve_rules(variant="shadow").spec_hash
        v2_1_spec = verify_v2_1_spec_hash()
        v2_1_protocol = verify_v2_1_protocol_hash()
        validate_v2_1_forward_anchor()
        anchor = verify_v2_1_forward_anchor_hash()
        m1_protocol = verify_protocol_hash()
    except Exception as exc:  # noqa: BLE001 - identity must fail closed
        raise M12AuditError(f"frozen identity verification failed: {exc}") from exc
    actual = {
        "v1_control_sha256": control,
        "v1_shadow_sha256": shadow,
        "v2_1_spec_sha256": v2_1_spec,
        "v2_1_protocol_sha256": v2_1_protocol,
        "v2_1_forward_anchor_sha256": anchor,
        "v1_m1_protocol_sha256": m1_protocol,
    }
    expected = {
        "v1_control_sha256": APPROVED_V1_CONTROL_SHA256,
        "v1_shadow_sha256": SHADOW_SPEC_HASH,
        "v2_1_spec_sha256": APPROVED_V2_1_SPEC_SHA256,
        "v2_1_protocol_sha256": APPROVED_V2_1_PROTOCOL_SHA256,
        "v2_1_forward_anchor_sha256": APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
        "v1_m1_protocol_sha256": M1_APPROVED_PROTOCOL_SHA256,
    }
    if actual != expected or control != CONTROL_SPEC_HASH:
        raise M12AuditError(f"frozen identity changed: expected={expected}, actual={actual}")
    if V21_FORWARD_ANCHOR_STATUS != "FROZEN_NOT_YET_STARTED":
        raise M12AuditError("V2.1 forward anchor is no longer FROZEN_NOT_YET_STARTED")
    return actual


def _immutable_paths() -> tuple[Path, ...]:
    return (
        PROJECT_ROOT / "research" / "m1" / "M1_DECISION.json",
        PROJECT_ROOT / "research" / "m1" / "M1_REPORT.md",
        PROJECT_ROOT / "research" / "v2_1" / "m1" / "V2_1_M1_ENGINEERING_REPORT.json",
        PROJECT_ROOT / "research" / "v2_1" / "m1" / "V2_1_M1_ENGINEERING_REPORT.md",
        PROJECT_ROOT / "research" / "v2_1" / "m1" / "V2_1_M1_TRACE_MANIFEST.json",
        PROJECT_ROOT / "research" / "v2_1" / "m1_1" / "PRESTART_EXTENSION_MANIFEST.json",
        PROJECT_ROOT / "research" / "v2_1" / "m1_1" / "V2_1_M1_1_WARM_START_AUDIT.json",
        PROJECT_ROOT / "research" / "v2_1" / "m1_1" / "V2_1_M1_1_WARM_START_AUDIT.md",
        PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_SPEC.yaml",
        PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_SPEC.sha256",
        PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_PROTOCOL.yaml",
        PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_PROTOCOL.sha256",
        PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_FORWARD_ANCHOR.yaml",
        PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_FORWARD_ANCHOR.sha256",
    )


def snapshot_immutable_artifacts() -> dict[str, str]:
    result: dict[str, str] = {}
    for path in _immutable_paths():
        if not path.is_file():
            raise M12AuditError(f"immutable artifact is missing: {path}")
        result[str(path.relative_to(PROJECT_ROOT))] = _file_sha256(path)
    return result


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_canonical(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fetch_get(
    name: str,
    endpoint: str,
    *,
    params: Mapping[str, Any],
    raw_name: str,
) -> tuple[ResponseRecord, bytes]:
    """Fetch and save one official read-only response."""
    if Path(raw_name).name != raw_name:
        raise M12AuditError("raw evidence filename must not contain a path")
    requested_url = f"{endpoint}?{urlencode(sorted((str(k), str(v)) for k, v in params.items()))}"
    fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        response = requests.get(
            endpoint,
            params=dict(params),
            headers={
                "Accept": "application/json",
                "User-Agent": "XS-LOWVOL-V2.1-M1.2 read-only audit",
            },
            timeout=90,
        )
    except requests.RequestException as exc:
        raise M12AuditError(f"official GET failed for {name}: {exc}") from exc
    content = bytes(response.content)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / raw_name
    raw_path.write_bytes(content)
    if response.status_code != 200:
        raise M12AuditError(f"official GET returned HTTP {response.status_code} for {name}")
    record = ResponseRecord(
        name=name,
        endpoint=endpoint,
        requested_url=requested_url,
        fetched_at_utc=fetched_at,
        status_code=response.status_code,
        content_sha256=_sha256_bytes(content),
        byte_count=len(content),
        local_path=str(raw_path.relative_to(PROJECT_ROOT)),
    )
    return record, content


def _json_payload(content: bytes, name: str) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise M12AuditError(f"{name} response is not valid UTF-8 JSON") from exc


def _rich_text(node: Any) -> str:
    if isinstance(node, Mapping):
        if node.get("node") == "text":
            return str(node.get("text", ""))
        children = node.get("child", ())
        if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
            return " ".join(part for part in (_rich_text(child) for child in children) if part)
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return " ".join(part for part in (_rich_text(child) for child in node) if part)
    return ""


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value).replace("\xa0", " ")).strip()


def parse_announcement(content: bytes) -> dict[str, Any]:
    payload = _json_payload(content, "announcement_cms")
    if not isinstance(payload, Mapping) or payload.get("code") != "000000":
        raise M12AuditError("Binance CMS announcement response was not successful")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise M12AuditError("Binance CMS announcement response has no data object")
    if data.get("code") != ANNOUNCEMENT_CODE or data.get("title") != ANNOUNCEMENT_TITLE:
        raise M12AuditError("Binance CMS returned an unexpected announcement identity")
    body = data.get("body")
    if not isinstance(body, str):
        raise M12AuditError("Binance CMS announcement body is missing")
    try:
        body_tree = json.loads(body)
    except json.JSONDecodeError as exc:
        raise M12AuditError("Binance CMS announcement body is not rich-text JSON") from exc
    text = _normalized_text(_rich_text(body_tree))
    settlement_re = re.compile(
        r"Binance Futures will close all positions and conduct an automatic settlement on the "
        r"(TONUSDT USDⓈ-M Perpetual Contracts) at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \(UTC\)\."
    )
    order_re = re.compile(
        r"Users are not allowed to open new orders for the aforementioned contract\(s\) "
        r"starting from (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \(UTC\)\."
    )
    settlement_match = settlement_re.search(text)
    order_match = order_re.search(text)
    if settlement_match is None or order_match is None:
        raise M12AuditError("required TONUSDT lifecycle facts were not found in official announcement")
    settlement_text = settlement_match.group(2)
    restriction_text = order_match.group(1)
    settlement_dt = datetime.strptime(settlement_text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    restriction_dt = datetime.strptime(restriction_text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    settlement_ms = int(settlement_dt.timestamp() * 1000)
    restriction_ms = int(restriction_dt.timestamp() * 1000)
    if settlement_ms != CONTRACT_SETTLEMENT_MS or restriction_ms != NEW_ORDER_RESTRICTION_MS:
        raise M12AuditError("official announcement lifecycle timestamps changed")
    if settlement_match.group(1) != "TONUSDT USDⓈ-M Perpetual Contracts":
        raise M12AuditError("official announcement contract identity changed")
    return {
        "title": str(data["title"]),
        "announcement_code": str(data["code"]),
        "cms_numeric_id": int(data["id"]),
        "published_at_utc": _utc_iso(int(data["publishDate"])),
        "source_page_url": ANNOUNCEMENT_PAGE_URL,
        "source_api_url": ANNOUNCEMENT_API_URL,
        "contract_text": settlement_match.group(1),
        "automatic_settlement_utc": _utc_iso(settlement_ms),
        "automatic_settlement_timestamp_ms": settlement_ms,
        "order_restriction_utc": _utc_iso(restriction_ms),
        "order_restriction_timestamp_ms": restriction_ms,
        "settlement_source_excerpt": settlement_match.group(0),
        "order_restriction_source_excerpt": order_match.group(0),
        "body_text_sha256": _sha256_bytes(text.encode("utf-8")),
    }


def parse_funding(content: bytes) -> list[dict[str, Any]]:
    payload = _json_payload(content, "ton_funding_history")
    if not isinstance(payload, list) or not payload:
        raise M12AuditError("TONUSDT funding response is empty or malformed")
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise M12AuditError("TONUSDT funding response contains a malformed row")
        try:
            timestamp_ms = int(item["fundingTime"])
            funding_rate = str(item["fundingRate"])
            mark_price = str(item["markPrice"])
        except (KeyError, TypeError, ValueError) as exc:
            raise M12AuditError("TONUSDT funding row is missing required fields") from exc
        if not math.isfinite(float(funding_rate)) or not math.isfinite(float(mark_price)):
            raise M12AuditError("TONUSDT funding row contains a non-finite value")
        rows.append(
            {
                "symbol": str(item.get("symbol", "")).upper(),
                "funding_time_ms": timestamp_ms,
                "funding_time_utc": _utc_iso(timestamp_ms),
                "funding_rate": funding_rate,
                "mark_price": mark_price,
                "rate_type": item.get("rateType"),
            }
        )
    rows.sort(key=lambda row: row["funding_time_ms"])
    timestamps = [row["funding_time_ms"] for row in rows]
    if len(timestamps) != len(set(timestamps)):
        raise M12AuditError("TONUSDT funding response contains duplicate timestamps")
    if timestamps[0] < FUNDING_START_MS or timestamps[-1] > FUNDING_END_MS:
        raise M12AuditError("TONUSDT funding response escaped the requested time window")
    if not any(row["funding_time_ms"] < CONTRACT_SETTLEMENT_MS for row in rows):
        raise M12AuditError("TONUSDT funding response has no event before contract settlement")
    return rows


def _parse_kline_rows(content: bytes, name: str) -> list[dict[str, Any]]:
    payload = _json_payload(content, name)
    if not isinstance(payload, list) or not payload:
        raise M12AuditError(f"{name} response is empty or malformed")
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) < 9:
            raise M12AuditError(f"{name} contains a malformed kline row")
        try:
            open_time_ms = int(item[0])
            close_time_ms = int(item[6])
            volume = str(item[5])
            quote_volume = str(item[7])
            trade_count = int(item[8])
        except (TypeError, ValueError) as exc:
            raise M12AuditError(f"{name} contains non-numeric kline fields") from exc
        rows.append(
            {
                "open_time_ms": open_time_ms,
                "open_time_utc": _utc_iso(open_time_ms),
                "open": str(item[1]),
                "high": str(item[2]),
                "low": str(item[3]),
                "close": str(item[4]),
                "volume": volume,
                "close_time_ms": close_time_ms,
                "close_time_utc": _utc_iso(close_time_ms),
                "quote_volume": quote_volume,
                "trade_count": trade_count,
            }
        )
    rows.sort(key=lambda row: row["open_time_ms"])
    return rows


def summarize_market_evidence(
    regular_rows: Sequence[Mapping[str, Any]],
    mark_rows: Sequence[Mapping[str, Any]],
    daily_rows: Sequence[Mapping[str, Any]],
    frozen_history: Any,
) -> dict[str, Any]:
    actual_trade_rows = [
        row
        for row in regular_rows
        if float(row["volume"]) > 0 or int(row["trade_count"]) > 0
    ]
    boundary_rows = [
        row for row in regular_rows if int(row["open_time_ms"]) == CONTRACT_SETTLEMENT_MS
    ]
    strict_after_rows = [
        row
        for row in actual_trade_rows
        if int(row["open_time_ms"]) > CONTRACT_SETTLEMENT_MS
    ]
    boundary = dict(boundary_rows[0]) if boundary_rows else None
    frozen_post_settlement = [
        bar
        for bar in frozen_history.daily_bars
        if bar.day > datetime.fromtimestamp(CONTRACT_SETTLEMENT_MS / 1000, UTC).date()
        and float(bar.quote_volume) == 0.0
        and bar.open == bar.high == bar.low == bar.close
    ]
    return {
        "regular_1m_row_count": len(regular_rows),
        "mark_price_1m_row_count": len(mark_rows),
        "daily_row_count": len(daily_rows),
        "last_actual_trade_kline_open_time_utc": (
            _utc_iso(max(int(row["open_time_ms"]) for row in actual_trade_rows))
            if actual_trade_rows
            else None
        ),
        "last_actual_trade_kline_row": dict(actual_trade_rows[-1]) if actual_trade_rows else None,
        "settlement_boundary_kline": boundary,
        "boundary_kline_has_real_trades": bool(
            boundary and (float(boundary["volume"]) > 0 or int(boundary["trade_count"]) > 0)
        ),
        "real_trade_kline_rows_starting_strictly_after_settlement": len(strict_after_rows),
        "real_trade_status_after_settlement": (
            "NO_KLINE_BUCKET_STARTING_STRICTLY_AFTER_SETTLEMENT;"
            " THE_09_00_BOUNDARY_BUCKET_HAS_REAL_TRADES"
        if not strict_after_rows and boundary else
            "REAL_TRADE_BUCKETS_EXIST_AFTER_SETTLEMENT"
            if strict_after_rows else
            "NO_REAL_TRADE_EVIDENCE"
        ),
        "subminute_trade_ordering": (
            "NOT_EXPOSED_BY_1M_KLINE_API; 09:00 BUCKET STARTS AT THE OFFICIAL"
            " SETTLEMENT INSTANT"
        ),
        "daily_rows": [dict(row) for row in daily_rows],
        "frozen_history_lifecycle": _canonical(asdict(frozen_history.lifecycle)),
        "frozen_history_last_daily_bar_utc": frozen_history.daily_bars[-1].day.isoformat(),
        "frozen_history_post_settlement_flat_zero_volume_day_count": len(frozen_post_settlement),
        "frozen_history_first_post_settlement_flat_zero_volume_day": (
            frozen_post_settlement[0].day.isoformat() if frozen_post_settlement else None
        ),
        "flat_zero_volume_interpretation": (
            "ARCHIVAL_ONLY_NOT_EVIDENCE_OF_TRADABILITY; official contract removal"
            " and absence of later real-trade kline buckets control lifecycle"
        ),
    }


def classify_funding_lifecycle_root_cause(
    *,
    last_funding_ms: int | None,
    funding_interval_hours: float | None,
    contract_termination_ms: int | None,
    holding_end_ms: int | None,
    actual_funding_times_ms: Iterable[int] = (),
) -> dict[str, Any]:
    """Classify only the lifecycle/funding boundary, fail-closed.

    The helper intentionally distinguishes "no required settlement" from the
    three audit root-cause classes.  It is independent of the frozen
    ``validate_funding_coverage_for_holds`` implementation so the four
    boundary cases can be tested without changing that validator.
    """
    actual = {int(value) for value in actual_funding_times_ms}
    if last_funding_ms is None or funding_interval_hours is None or holding_end_ms is None:
        return {
            "classification": "ROOT_CAUSE_UNRESOLVED",
            "required_funding_missing": False,
            "next_scheduled_funding_ms": None,
            "reason": "last funding, interval, or V1 holding termination is unknown",
        }
    if not math.isfinite(float(funding_interval_hours)) or float(funding_interval_hours) <= 0:
        return {
            "classification": "ROOT_CAUSE_UNRESOLVED",
            "required_funding_missing": False,
            "next_scheduled_funding_ms": None,
            "reason": "funding interval is invalid or unknown",
        }
    next_ms = int(round(int(last_funding_ms) + float(funding_interval_hours) * 3_600_000.0))
    next_actual = next_ms in actual or any(
        abs(timestamp - next_ms) <= 10 for timestamp in actual
    )
    if contract_termination_ms is None:
        return {
            "classification": "ROOT_CAUSE_UNRESOLVED",
            "required_funding_missing": False,
            "next_scheduled_funding_ms": next_ms,
            "next_scheduled_funding_utc": _utc_iso(next_ms),
            "reason": "official contract termination is unknown",
        }
    if holding_end_ms <= next_ms:
        return {
            "classification": None,
            "required_funding_missing": False,
            "next_scheduled_funding_ms": next_ms,
            "next_scheduled_funding_utc": _utc_iso(next_ms),
            "reason": "holding ends before or at the next scheduled settlement",
            "next_scheduled_funding_observed": next_actual,
        }
    if next_ms <= contract_termination_ms:
        if next_actual:
            return {
                "classification": None,
                "required_funding_missing": False,
                "next_scheduled_funding_ms": next_ms,
                "next_scheduled_funding_utc": _utc_iso(next_ms),
                "reason": "next scheduled settlement is present",
            }
        return {
            "classification": "REAL_MISSING_REQUIRED_FUNDING_SETTLEMENT",
            "required_funding_missing": True,
            "next_scheduled_funding_ms": next_ms,
            "next_scheduled_funding_utc": _utc_iso(next_ms),
            "reason": "a scheduled settlement falls inside the valid contract lifetime and is absent",
        }
    return {
        "classification": "LIFECYCLE_BOUNDARY_FALSE_POSITIVE_CONFIRMED",
        "required_funding_missing": False,
        "next_scheduled_funding_ms": next_ms,
        "next_scheduled_funding_utc": _utc_iso(next_ms),
        "reason": "next scheduled settlement is after contract termination while V1 holding remains open",
        "next_scheduled_funding_observed": next_actual,
    }


def run_boundary_diagnostic_cases() -> list[dict[str, Any]]:
    cases = (
        (
            "case_1_contract_ends_before_next_settlement",
            dict(
                last_funding_ms=CONTRACT_SETTLEMENT_MS - 3_600_000,
                funding_interval_hours=4.0,
                contract_termination_ms=CONTRACT_SETTLEMENT_MS,
                holding_end_ms=CONTRACT_SETTLEMENT_MS,
            ),
            "NO_REQUIRED_FUNDING_MISSING",
        ),
        (
            "case_2_active_contract_missing_required_settlement",
            dict(
                last_funding_ms=CONTRACT_SETTLEMENT_MS - 3_600_000,
                funding_interval_hours=4.0,
                contract_termination_ms=CONTRACT_SETTLEMENT_MS + 4 * 3_600_000,
                holding_end_ms=CONTRACT_SETTLEMENT_MS + 5 * 3_600_000,
            ),
            "REAL_MISSING_REQUIRED_FUNDING_SETTLEMENT",
        ),
        (
            "case_3_unknown_contract_termination",
            dict(
                last_funding_ms=CONTRACT_SETTLEMENT_MS - 3_600_000,
                funding_interval_hours=4.0,
                contract_termination_ms=None,
                holding_end_ms=CONTRACT_SETTLEMENT_MS + 5 * 3_600_000,
            ),
            "ROOT_CAUSE_UNRESOLVED",
        ),
        (
            "case_4_holding_ends_before_next_settlement",
            dict(
                last_funding_ms=CONTRACT_SETTLEMENT_MS - 3_600_000,
                funding_interval_hours=4.0,
                contract_termination_ms=CONTRACT_SETTLEMENT_MS + 5 * 3_600_000,
                holding_end_ms=CONTRACT_SETTLEMENT_MS + 1 * 3_600_000,
            ),
            "NO_REQUIRED_FUNDING_MISSING",
        ),
    )
    result: list[dict[str, Any]] = []
    for name, args, expected in cases:
        assessed = classify_funding_lifecycle_root_cause(**args)
        observed = (
            "NO_REQUIRED_FUNDING_MISSING"
            if assessed["classification"] is None and not assessed["required_funding_missing"]
            else assessed["classification"]
        )
        result.append(
            {
                "case": name,
                "expected": expected,
                "observed": observed,
                "passed": observed == expected,
                "assessment": assessed,
            }
        )
    if not all(row["passed"] for row in result):
        raise M12AuditError("one or more independent lifecycle boundary cases failed")
    return result


def _extract_v1_ton_holding(dataset: Mapping[str, Any]) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    histories = tuple(dataset["usable_histories"])
    history = next((item for item in histories if item.symbol.upper() == SYMBOL), None)
    if history is None:
        raise M12AuditError("TONUSDT is missing from the frozen usable V1 history")
    schedule = build_stateful_schedule(
        histories,
        data_start=date(2020, 1, 1),
        data_end=date(2026, 8, 31),
    )
    accounting = simulate_frozen_portfolio(
        histories,
        schedule.signals,
        variant="control",
        scenario="COST_1X",
        data_start=date(2020, 1, 1),
        data_end=date(2026, 8, 31),
    )
    intervals = [interval for interval in accounting.holding_intervals if interval.symbol == SYMBOL]
    if not intervals:
        raise M12AuditError("V1 Control accounting emitted no TONUSDT holding interval")
    interval = max(intervals, key=lambda item: item.entry_timestamp_ms)
    entry_day = datetime.fromtimestamp(interval.entry_timestamp_ms / 1000.0, UTC).date()
    matching_attempts = [
        attempt
        for attempt in schedule.attempts
        if attempt.status == "SUCCESS"
        and attempt.execution_day == entry_day
        and SYMBOL in attempt.target_symbols
    ]
    if len(matching_attempts) != 1:
        raise M12AuditError("V1 Control TONUSDT entry does not map to one successful schedule attempt")
    attempt = matching_attempts[0]
    signal = schedule.signals.get(attempt.signal_day)
    if signal is None:
        raise M12AuditError("V1 Control TONUSDT entry has no PIT signal")
    if SYMBOL in signal.longs:
        direction = 1
        direction_text = "LONG"
    elif SYMBOL in signal.shorts:
        direction = -1
        direction_text = "SHORT"
    else:
        raise M12AuditError("V1 Control TONUSDT entry direction is not in the PIT signal")
    if history.lifecycle.delisted_at is None:
        exit_reason = "NO_FORCED_LIFECYCLE_EXIT; DATA_WINDOW_TERMINAL_MARK"
        forced_exit = False
    else:
        exit_reason = "FORCED_LIFECYCLE_EXIT_AT_TERMINAL_COMPLETED_CLOSE"
        forced_exit = True
    holding = {
        "symbol": SYMBOL,
        "interval_semantics": "(entry_timestamp, exit_timestamp]",
        "entry_timestamp_ms": interval.entry_timestamp_ms,
        "entry_timestamp_utc": _utc_iso(interval.entry_timestamp_ms),
        "exit_timestamp_ms": interval.exit_timestamp_ms,
        "exit_timestamp_utc": _utc_iso(interval.exit_timestamp_ms),
        "signal_day": attempt.signal_day.isoformat(),
        "execution_day": attempt.execution_day.isoformat(),
        "direction": direction,
        "direction_text": direction_text,
        "forced_lifecycle_exit": forced_exit,
        "lifecycle_exit_reason": exit_reason,
        "frozen_history_lifecycle": _canonical(asdict(history.lifecycle)),
    }
    return history, holding, {
        "schedule_attempt": _canonical(asdict(attempt)),
        "accounting_complete": accounting.complete,
        "accounting_issue_count": len(accounting.issues),
        "accounting_data_window_end": "2026-08-31",
    }


def _current_validator_week_evidence(history: Any, interval: HoldingInterval) -> dict[str, Any]:
    week_start_ms = _utc_day_start_ms(REQUIRED_WEEK_START)
    week_end_ms = _utc_day_start_ms(REQUIRED_WEEK_END + timedelta(days=1)) - 1
    if interval.entry_timestamp_ms >= week_end_ms or interval.exit_timestamp_ms <= week_start_ms:
        raise M12AuditError("TONUSDT holding does not intersect the required week")
    clipped = HoldingInterval(
        SYMBOL,
        max(interval.entry_timestamp_ms, week_start_ms),
        min(interval.exit_timestamp_ms, week_end_ms),
    )
    report = validate_funding_coverage_for_holds((history,), (clipped,))
    return {
        "required_week_start": REQUIRED_WEEK_START.isoformat(),
        "required_week_end": REQUIRED_WEEK_END.isoformat(),
        "clipped_holding_entry_timestamp_ms": clipped.entry_timestamp_ms,
        "clipped_holding_entry_timestamp_utc": _utc_iso(clipped.entry_timestamp_ms),
        "clipped_holding_exit_timestamp_ms": clipped.exit_timestamp_ms,
        "clipped_holding_exit_timestamp_utc": _utc_iso(clipped.exit_timestamp_ms),
        "holding_intersects_required_week": True,
        "validator_result": "FAIL" if report.hold_issues else "PASS",
        "validator_issue_count": len(report.hold_issues),
        "validator_issues": [_canonical(asdict(issue)) for issue in report.hold_issues],
        "validator_uses_official_contract_termination_timestamp": False,
        "validator_lifecycle_termination_from_frozen_history": (
            _canonical(asdict(history.lifecycle)).get("delisted_at")
        ),
        "failure_mechanism": (
            "V1 validator uses the frozen holding exit and funding event sequence;"
            " frozen history has no lifecycle termination timestamp, so it asks"
            " whether the next 4-hour settlement is inside the extended interval"
        ),
    }


def _legacy_m1_1_summary() -> dict[str, Any]:
    path = PROJECT_ROOT / "research" / "v2_1" / "m1_1" / "V2_1_M1_1_WARM_START_AUDIT.json"
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise M12AuditError("cannot read the frozen M1.1 artifact") from exc
    return {
        "final_decision": source.get("final_decision"),
        "completeness": source.get("completeness"),
        "failed_gates": source.get("failed_gates"),
        "gates": source.get("gates"),
        "required_window_intersection_symbols": source.get("global_funding_diagnostic", {}).get(
            "required_window_intersection_symbols", []
        ),
    }


def _fetch_official_evidence() -> tuple[dict[str, Any], dict[str, Any]]:
    announcement_record, announcement_content = _fetch_get(
        "announcement_cms",
        ANNOUNCEMENT_API_URL,
        params={"articleCode": ANNOUNCEMENT_CODE},
        raw_name="announcement_cms.json",
    )
    announcement = parse_announcement(announcement_content)
    funding_record, funding_content = _fetch_get(
        "ton_funding_history",
        FUNDING_ENDPOINT,
        params={
            "symbol": SYMBOL,
            "startTime": FUNDING_START_MS,
            "endTime": FUNDING_END_MS,
            "limit": 1000,
        },
        raw_name="TONUSDT_funding_2026-06-20_2026-06-24.json",
    )
    funding = parse_funding(funding_content)
    regular_record, regular_content = _fetch_get(
        "ton_regular_1m_klines",
        KLINE_ENDPOINT,
        params={
            "symbol": SYMBOL,
            "interval": "1m",
            "startTime": MARKET_START_MS,
            "endTime": MARKET_END_MS,
            "limit": 1000,
        },
        raw_name="TONUSDT_regular_1m_2026-06-23T07-10Z.json",
    )
    regular = _parse_kline_rows(regular_content, "ton_regular_1m_klines")
    mark_record, mark_content = _fetch_get(
        "ton_mark_price_1m_klines",
        MARK_KLINE_ENDPOINT,
        params={
            "symbol": SYMBOL,
            "interval": "1m",
            "startTime": MARKET_START_MS,
            "endTime": MARKET_END_MS,
            "limit": 1000,
        },
        raw_name="TONUSDT_mark_price_1m_2026-06-23T07-10Z.json",
    )
    mark = _parse_kline_rows(mark_content, "ton_mark_price_1m_klines")
    daily_record, daily_content = _fetch_get(
        "ton_daily_klines",
        KLINE_ENDPOINT,
        params={
            "symbol": SYMBOL,
            "interval": "1d",
            "startTime": DAILY_START_MS,
            "endTime": DAILY_END_MS,
            "limit": 1000,
        },
        raw_name="TONUSDT_daily_2026-06-22_2026-06-24.json",
    )
    daily = _parse_kline_rows(daily_content, "ton_daily_klines")
    if regular[0]["open_time_ms"] > MARKET_START_MS or regular[-1]["open_time_ms"] < MARKET_END_MS:
        raise M12AuditError("regular 1m evidence does not cover the requested boundary")
    if mark[0]["open_time_ms"] > MARKET_START_MS or mark[-1]["open_time_ms"] < MARKET_END_MS:
        raise M12AuditError("mark-price 1m evidence does not cover the requested boundary")
    if [row["open_time_ms"] for row in daily] != [DAILY_START_MS, DAILY_START_MS + 86_400_000, DAILY_END_MS]:
        raise M12AuditError("daily evidence does not contain exactly 2026-06-22 through 2026-06-24")
    request_records = {
        record.name: record.as_dict()
        for record in (announcement_record, funding_record, regular_record, mark_record, daily_record)
    }
    return (
        {
            "announcement": announcement,
            "funding_history": funding,
            "regular_1m_rows": regular,
            "mark_price_1m_rows": mark,
            "daily_rows": daily,
        },
        request_records,
    )


def _build_provenance_manifest(
    evidence: Mapping[str, Any],
    request_records: Mapping[str, Any],
    dataset: Mapping[str, Any],
    market_summary: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "XS-LOWVOL-V2.1-M1.2-TON-OFFICIAL-PROVENANCE-V1",
        "audit_id": AUDIT_ID,
        "retrieval_scope": "OFFICIAL_BINANCE_READ_ONLY_GETS",
        "requests": request_records,
        "announcement": evidence["announcement"],
        "funding_request_window": {
            "start_utc": _utc_iso(FUNDING_START_MS),
            "end_utc": _utc_iso(FUNDING_END_MS),
            "row_count": len(evidence["funding_history"]),
        },
        "funding_history": evidence["funding_history"],
        "market_evidence_window": {
            "start_utc": _utc_iso(MARKET_START_MS),
            "end_utc": _utc_iso(MARKET_END_MS),
            "daily_start_utc": _utc_iso(DAILY_START_MS),
            "daily_end_utc": _utc_iso(DAILY_END_MS),
        },
        "market_summary": market_summary,
        "frozen_dataset_reference": dataset_metadata(dataset),
    }


def _build_markdown(report: Mapping[str, Any]) -> str:
    time_points = report["time_points"]
    holding = report["v1_control_holding"]
    identity = report["identities"]
    root = report["root_cause_assessment"]
    lines = [
        "# XS-LOWVOL V2.1-M1.2 TONUSDT Root-Cause Audit",
        "",
        f"- Decision: `{report['decision']}`",
        f"- Audit ID: `{report['audit_id']}`",
        f"- Base commit: `{report['base_commit']}`",
        f"- Code commit: `{report['code_commit']}`",
        "",
        "## Frozen identities",
        "",
        f"- V1 Control SHA-256: `{identity['v1_control_sha256']}`",
        f"- V1 Shadow SHA-256: `{identity['v1_shadow_sha256']}`",
        f"- V2.1 Spec SHA-256: `{identity['v2_1_spec_sha256']}`",
        f"- V2.1 Protocol SHA-256: `{identity['v2_1_protocol_sha256']}`",
        f"- V2.1 Forward Anchor SHA-256: `{identity['v2_1_forward_anchor_sha256']}`",
        f"- Anchor status: `{report['anchor_status']}`",
        "",
        "## Official lifecycle evidence",
        "",
        f"- Announcement: [{report['official_provenance']['announcement']['title']}]({ANNOUNCEMENT_PAGE_URL})",
        f"- Contract text: `{report['official_provenance']['announcement']['contract_text']}`",
        f"- Automatic settlement: `{report['official_provenance']['announcement']['automatic_settlement_utc']}`",
        f"- New-order restriction: `{report['official_provenance']['announcement']['order_restriction_utc']}`",
        f"- Last official funding: `{time_points['T_last_funding']['utc']}`",
        f"- Next scheduled funding if still active: `{time_points['next_scheduled_funding_if_active']['utc']}`",
        "",
        "## Frozen V1 Control interval",
        "",
        f"- Entry: `{holding['entry_timestamp_utc']}` ({holding['entry_timestamp_ms']})",
        f"- Exit: `{holding['exit_timestamp_utc']}` ({holding['exit_timestamp_ms']})",
        f"- Signal day / execution day: `{holding['signal_day']}` / `{holding['execution_day']}`",
        f"- Direction: `{holding['direction_text']}`",
        f"- Lifecycle exit: `{holding['lifecycle_exit_reason']}`",
        "",
        "## Root-cause decision",
        "",
        f"- Classification: `{report['root_cause_classification']}`",
        f"- Required funding missing: `{root['required_funding_missing']}`",
        f"- Validator extended beyond contract lifetime: `{report['validator_extended_beyond_contract_lifetime']}`",
        f"- Current validator result for required week: `{report['current_validator_week']['validator_result']}`",
        "",
        (
            "The 2026-06-28 week intersects the V1 interval because the frozen "
            "accounting carries TONUSDT from its 2025-07-10 entry through the "
            "2026-08-31 data-window terminal mark. The frozen lifecycle has no "
            "termination timestamp; the official 09:00 UTC contract settlement "
            "is therefore outside the old validator's inputs."
        ),
        "",
        "## Scope and immutability",
        "",
        "- No strategy, V2.1 Spec, Protocol, Anchor, V1 accounting, or funding validator was modified.",
        "- No funding zero-fill or synthetic settlement was used.",
        "- No historical result artifact was generated by this audit.",
        "- M1.1 remains `12/13`, W4 FAIL, W6 FAIL, NOT_READY.",
        "- No Forward evidence, first Forward signal, V2.1-M2, or live trading was started.",
        "",
    ]
    return "\n".join(lines)


def run_audit(*, approval: str = APPROVAL) -> dict[str, Any]:
    if approval != APPROVAL:
        raise M12AuditError(f"explicit approval must be exactly {APPROVAL!r}")
    if any(path.exists() for path in (REPORT_PATH, MARKDOWN_PATH, PROVENANCE_PATH)):
        raise M12AuditError("M1.2 output already exists; the fixed audit ID cannot be rerun")
    if _git_status_short():
        raise M12AuditError("working tree must be clean before the formal M1.2 audit")
    code_commit = _current_code_commit()
    if not _base_is_ancestor(code_commit):
        raise M12AuditError(f"approved base commit {BASE_COMMIT} is not an ancestor of {code_commit}")
    identities = verify_frozen_identity()
    immutable_before = snapshot_immutable_artifacts()
    diagnostic_cases = run_boundary_diagnostic_cases()
    dataset = load_verified_dataset()
    history, holding, accounting_meta = _extract_v1_ton_holding(dataset)
    interval = HoldingInterval(
        SYMBOL,
        int(holding["entry_timestamp_ms"]),
        int(holding["exit_timestamp_ms"]),
    )
    validator_week = _current_validator_week_evidence(history, interval)
    evidence, request_records = _fetch_official_evidence()
    announcement = evidence["announcement"]
    funding_history = evidence["funding_history"]
    last_funding = max(
        (row for row in funding_history if row["funding_time_ms"] < CONTRACT_SETTLEMENT_MS),
        key=lambda row: row["funding_time_ms"],
    )
    key_frozen_event = next(
        (
            event
            for event in history.funding_events
            if event.funding_time_ms == last_funding["funding_time_ms"]
        ),
        None,
    )
    if key_frozen_event is None:
        raise M12AuditError("last official funding event is absent from frozen TONUSDT history")
    interval_hours = float(key_frozen_event.funding_interval_hours)
    actual_lifetime_events = [
        row
        for row in funding_history
        if int(holding["entry_timestamp_ms"]) < row["funding_time_ms"] < CONTRACT_SETTLEMENT_MS
    ]
    root_assessment = classify_funding_lifecycle_root_cause(
        last_funding_ms=last_funding["funding_time_ms"],
        funding_interval_hours=interval_hours,
        contract_termination_ms=CONTRACT_SETTLEMENT_MS,
        holding_end_ms=int(holding["exit_timestamp_ms"]),
        actual_funding_times_ms=(row["funding_time_ms"] for row in funding_history),
    )
    if root_assessment["classification"] is None:
        raise M12AuditError("root-cause classifier returned no root cause for formal TON audit")
    market_summary = summarize_market_evidence(
        _parse_kline_rows(
            (RAW_DIR / "TONUSDT_regular_1m_2026-06-23T07-10Z.json").read_bytes(),
            "ton_regular_1m_klines",
        ),
        _parse_kline_rows(
            (RAW_DIR / "TONUSDT_mark_price_1m_2026-06-23T07-10Z.json").read_bytes(),
            "ton_mark_price_1m_klines",
        ),
        _parse_kline_rows(
            (RAW_DIR / "TONUSDT_daily_2026-06-22_2026-06-24.json").read_bytes(),
            "ton_daily_klines",
        ),
        history,
    )
    time_points = {
        "T_last_funding": {
            "timestamp_ms": last_funding["funding_time_ms"],
            "utc": last_funding["funding_time_utc"],
        },
        "T_contract_auto_settlement": {
            "timestamp_ms": announcement["automatic_settlement_timestamp_ms"],
            "utc": announcement["automatic_settlement_utc"],
        },
        "T_v1_holding_exit": {
            "timestamp_ms": holding["exit_timestamp_ms"],
            "utc": holding["exit_timestamp_utc"],
        },
        "next_scheduled_funding_if_active": {
            "timestamp_ms": root_assessment["next_scheduled_funding_ms"],
            "utc": root_assessment["next_scheduled_funding_utc"],
        },
        "relationship": {
            "last_funding_before_contract_termination": last_funding["funding_time_ms"] < CONTRACT_SETTLEMENT_MS,
            "next_scheduled_funding_after_contract_termination": root_assessment["next_scheduled_funding_ms"] > CONTRACT_SETTLEMENT_MS,
            "v1_holding_exit_after_contract_termination": holding["exit_timestamp_ms"] > CONTRACT_SETTLEMENT_MS,
            "funding_interval_hours_from_frozen_v1_event": interval_hours,
        },
    }
    provenance_manifest = _build_provenance_manifest(
        evidence,
        request_records,
        dataset,
        market_summary,
    )
    _write_json(PROVENANCE_PATH, provenance_manifest)
    immutable_after = snapshot_immutable_artifacts()
    if immutable_before != immutable_after:
        raise M12AuditError("an old M1/M1.1/spec/protocol/anchor artifact changed during audit")
    legacy_m1_1 = _legacy_m1_1_summary()
    decision = (
        "V2.1-M1.2 ROOT_CAUSE UNRESOLVED"
        if root_assessment["classification"] == "ROOT_CAUSE_UNRESOLVED"
        else "V2.1-M1.2 ROOT_CAUSE CONFIRMED"
    )
    report: dict[str, Any] = {
        "schema": "XS-LOWVOL-V2.1-M1.2-TON-ROOT-CAUSE-V1",
        "audit_id": AUDIT_ID,
        "decision": decision,
        "root_cause_classification": root_assessment["classification"],
        "base_commit": BASE_COMMIT,
        "code_commit": code_commit,
        "anchor_status": V21_FORWARD_ANCHOR_STATUS,
        "identities": identities,
        "dataset": dataset_metadata(dataset),
        "official_provenance": {
            "manifest": str(PROVENANCE_PATH.relative_to(PROJECT_ROOT)),
            "announcement": announcement,
            "request_names": sorted(request_records),
        },
        "time_points": time_points,
        "last_official_funding": last_funding,
        "actual_funding_events_inside_economic_contract_lifetime": actual_lifetime_events,
        "v1_control_holding": holding,
        "v1_accounting_metadata": accounting_meta,
        "validator_extended_beyond_contract_lifetime": holding["exit_timestamp_ms"] > CONTRACT_SETTLEMENT_MS,
        "current_validator_week": validator_week,
        "market_lifecycle_evidence": market_summary,
        "root_cause_assessment": root_assessment,
        "diagnostic_boundary_cases": diagnostic_cases,
        "legacy_m1_1_status_unchanged": legacy_m1_1,
        "immutability": {
            "before": immutable_before,
            "after": immutable_after,
            "unchanged": immutable_before == immutable_after,
        },
        "warm_start": {
            "status": "NOT_READY_UNCHANGED",
            "required_week_result_remains": "12_OF_13; W4_FAIL; W6_FAIL",
        },
        "scope": {
            "strategy_modified": False,
            "v2_1_spec_modified": False,
            "v2_1_protocol_modified": False,
            "v2_1_anchor_modified": False,
            "v1_accounting_modified": False,
            "funding_validator_modified": False,
            "funding_zero_fill_used": False,
            "synthetic_settlement_used": False,
            "historical_result_artifact_created": False,
            "forward_evidence_created": False,
            "first_forward_signal_created": False,
            "parameter_optimization_performed": False,
            "v2_1_m2_started": False,
            "live_trading": False,
        },
        "safety": {
            "live_trading": False,
            "paper_only": True,
            "http_method": "GET-only",
            "official_request_count": len(request_records),
        },
    }
    assert_no_historical_result_fields(report)
    _write_json(REPORT_PATH, report)
    MARKDOWN_PATH.write_text(_build_markdown(report), encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="XS-LOWVOL V2.1-M1.2 TONUSDT root-cause audit")
    parser.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    try:
        report = run_audit(approval=args.approval)
    except M12AuditError as exc:
        print(f"M1.2 INVALID: {exc}")
        return 1
    print(report["decision"])
    print(f"root_cause_classification={report['root_cause_classification']}")
    print(f"report={REPORT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"provenance={PROVENANCE_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
