"""XS-LOWVOL V2.1-M1.4 current pre-start bridge.

This module is deliberately a narrow state bridge.  It starts from the
accepted M1.3 parent checkpoint, fetches only completed Binance USD-M data in
the fixed 2026-09-01 through 2026-09-18 window, and never calls the historical
M1 scheduler or portfolio replay.  The output is state initialization only;
it is not Forward evidence and it does not publish runtime evidence.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.xs_lowvol_spec import resolve_rules, strategy_spec_hash
from src.xs_lowvol_v2_1_anchor import (
    APPROVED_V1_CONTROL_SHA256,
    APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_1_PROTOCOL_SHA256,
    APPROVED_V2_1_SPEC_SHA256,
    V21_FORWARD_ANCHOR_STATUS,
    validate_v2_1_forward_anchor,
    verify_v2_1_forward_anchor_hash,
)
from src.xs_lowvol_v2_1_risk import ControlWeeklyReturn, evaluate_risk_scale
from src.xs_lowvol_v2_1_spec import verify_v2_1_spec_hash

from .m1_b import _build_universe_fast, _signal_from_universe
from .m1_protocol import M1_APPROVED_PROTOCOL_SHA256, verify_protocol_hash
from .v2_1_m1_3_parent_reconstruction import (
    DEFAULT_OVERLAY,
    assert_prestart_state_schema as assert_m13_prestart_state_schema,
)
from .v2_1_m1_engineering import (
    EXPECTED_DATASET_SHA256,
    EXPECTED_NORMALIZED_DATASET_SHA256,
    load_verified_dataset,
)
from .xs_history import DailyBar, FundingEvent, LifecycleRecord, SymbolHistory
from src.xs_lowvol_v2_1_lifecycle import apply_lifecycle_overlay


PROJECT_ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc

M14_BASE_COMMIT = "69fe4156c4ec024318c498ce4ba6e591c078e125"
M14_RUN_ID = "XS-LOWVOL-V2.1-M1.4-PRESTART-BRIDGE-1"
M14_APPROVAL = "START V2.1-M1.4"
M14_OUTPUT_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_4"
M14_RAW_DIR = PROJECT_ROOT / "data" / "v2_1_m1_4_prestart_extension"
M14_CHECKPOINT_PATH = M14_OUTPUT_DIR / "M1_3_ACCEPTED_STATE_CHECKPOINT.json"
M14_EXTENSION_MANIFEST_PATH = M14_OUTPUT_DIR / "PRESTART_EXTENSION_MANIFEST.json"
M14_LIFECYCLE_PATH = M14_OUTPUT_DIR / "PRESTART_LIFECYCLE_EXTENSION.json"
M14_RISK_PATH = M14_OUTPUT_DIR / "CURRENT_LATEST_13_RISK_INPUT.json"
M14_PARENT_STATE_PATH = M14_OUTPUT_DIR / "CURRENT_PARENT_STATE.json"
M14_REPORT_PATH = M14_OUTPUT_DIR / "V2_1_M1_4_PRESTART_BRIDGE.json"
M14_MARKDOWN_PATH = M14_OUTPUT_DIR / "V2_1_M1_4_PRESTART_BRIDGE.md"

PRESTART_CUTOFF = datetime(2026, 9, 19, tzinfo=UTC)
EXTENSION_START = date(2026, 9, 1)
EXTENSION_END = date(2026, 9, 18)
FROZEN_DATA_END = date(2026, 8, 31)
CHECKPOINT_TIMESTAMP = "2026-08-31T23:59:59.999Z"
LAST_ELIGIBLE_MS = int(PRESTART_CUTOFF.timestamp() * 1000) - 1
EXTENSION_START_MS = int(datetime.combine(EXTENSION_START, time.min, tzinfo=UTC).timestamp() * 1000)
EXTENSION_END_EXCLUSIVE_MS = int(PRESTART_CUTOFF.timestamp() * 1000)

V1_CONTROL_SHA256 = APPROVED_V1_CONTROL_SHA256
V1_SHADOW_SHA256 = "97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd"
V2_1_SPEC_SHA256 = APPROVED_V2_1_SPEC_SHA256
V2_1_PROTOCOL_SHA256 = APPROVED_V2_1_PROTOCOL_SHA256
FORWARD_ANCHOR_SHA256 = APPROVED_V2_1_FORWARD_ANCHOR_SHA256
M13_RESULT_COMMIT = "355dbfcb1dcf2d0e5f0fc1b84e478e788ec1b24a"
M13_CODE_COMMIT = "aebdda0169cddcdf013d24917515d4acfd1d5e74"
M13_OVERLAY_SHA256 = "7a6f183262580933c4802acb9454b0617c7c095b458b79f1e56fadb1c9ec64c1"

M13_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_3"
M13_ARTIFACT_SHA256 = {
    "FORCED_EXIT_PROVENANCE.json": "d5c24b374ed4e9dc0f50b59b1141431baa9d84061108ea73b9cdd1190bf3abcc",
    "FUNDING_LIFETIME_PROVENANCE.json": "1dc12019dce9f0f0d3c49e862029c345cc1237514ea813c1636ad9953f38649b",
    "HOLDING_PROVENANCE.json": "4502173c815618ec3eb79ca22a8c17348d822d7be4d9453f010fb749e3ec0025",
    "LIFECYCLE_OVERRIDE_MANIFEST.json": "d184ebb2c29e7890415d7eabc456b9fefcc9f749eb9660515f8c665074bc93da",
    "SCHEDULE_PROVENANCE.json": "dccb96269a82c9c21151c8cfd3f89f2092c6020ab1b0a3f0f28d737d1e76f35e",
    "V2_1_M1_3_PARENT_RECONSTRUCTION.json": "eaf37fdd32c8482ab24f707406564cad11c5fedd36814897dc5bfbc9bc5a1ab8",
    "V2_1_M1_3_PARENT_RECONSTRUCTION.md": "008d3f31732b00d0874b67a36c1049dc5c089bdf511f48ef94b216fcab9b585e",
    "WEEKLY_ACCOUNTING_PROVENANCE.json": "e19f2eebc16b3c2a471c5b3f9390bf2ca2ca9020155d1827a57757ac6af305aa",
}
M13_1_SCOPE_SHA256 = {
    "V2_1_M1_3_EVIDENCE_SCOPE.json": "5a9ce93ec6f40426d6c6c339b9781beb8b0da78e812dfec435306093d362ffd8",
    "V2_1_M1_3_EVIDENCE_SCOPE.md": "f98326c8244389bf22336d1738b0bace655b546ba0629a287c4df53b9ee58f53",
}

BINANCE_FAPI_BASE = "https://fapi.binance.com"
EXCHANGE_INFO_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/exchangeInfo"
FUNDING_INFO_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/fundingInfo"
KLINES_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/klines"
FUNDING_RATE_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/fundingRate"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")
_ALLOWED_WEEKLY_KEY = "weekly_return"
_ALLOWED_WEEKLY_COMPLETENESS_KEY = "weekly_return_complete"
_PRESTART_LABELS = (
    "PRESTART_STATE_INITIALIZATION_ONLY",
    "NOT_FORWARD",
    "NOT_VALIDATION_EVIDENCE",
)
_FORBIDDEN_M14_KEYS = frozenset(
    {
        "price_component_total",
        "funding_component_total",
        "transaction_cost_component_total",
        "long_component_total",
        "short_component_total",
    }
)


class M14Error(RuntimeError):
    """Fail-closed M1.4 bridge error."""


class M14IdentityError(M14Error):
    """Frozen identity, lineage, or immutable-artifact failure."""


class M14DataError(M14Error):
    """Pre-start data or state continuity failure."""


@dataclass(frozen=True)
class RequestRecord:
    name: str
    method: str
    endpoint: str
    params: Mapping[str, Any]
    request_url: str
    request_timestamp_utc: str
    http_status: int | None
    response_sha256: str | None
    local_path: str | None
    row_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    error: str | None = None


@dataclass(frozen=True)
class ExtensionSymbolResult:
    symbol: str
    daily_bars: tuple[DailyBar, ...]
    funding_events: tuple[FundingEvent, ...]
    daily_request: RequestRecord
    funding_request: RequestRecord
    exchange_info: Mapping[str, Any] | None
    missing_daily_days: tuple[date, ...]


@dataclass
class _BridgePosition:
    symbol: str
    direction: int
    notional: float
    entry_timestamp_ms: int
    mark_timestamp_ms: int
    mark_price: float


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    if isinstance(value, set):
        return sorted(_canonical(v) for v in value)
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _day_start_ms(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=UTC).timestamp() * 1000)


def _day_range(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        return ()
    return tuple(start + timedelta(days=index) for index in range((end - start).days + 1))


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def current_commit() -> str:
    value = _git_output("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise M14IdentityError("current commit is not a full SHA-1")
    return value


def _is_ancestor(base: str, commit: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, commit],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise M14IdentityError("git could not verify M1.4 base lineage")
    return result.returncode == 0


def _git_status_clean() -> bool:
    return _git_output("status", "--short") == ""


def _snapshot_paths(paths: Mapping[str, str]) -> dict[str, str]:
    actual: dict[str, str] = {}
    for name in paths:
        path = M13_DIR / name if name in M13_ARTIFACT_SHA256 else M13_DIR.parent / "m1_3_1" / name
        if not path.is_file():
            actual[name] = "MISSING"
        else:
            actual[name] = _file_sha256(path)
    return actual


def snapshot_m13_artifacts() -> dict[str, str]:
    return _snapshot_paths(M13_ARTIFACT_SHA256)


def snapshot_m13_1_scope() -> dict[str, str]:
    return _snapshot_paths(M13_1_SCOPE_SHA256)


def assert_prestart_state_schema(value: Any, path: str = "") -> None:
    """Apply the accepted state guard with the explicit weekly-return exception.

    M1.4 may retain a machine-readable weekly return solely for risk-state
    initialization.  It must be explicitly quarantined by the artifact
    labels; every other historical-result key remains rejected by the M1.3.1
    guard.
    """

    def strip_allowed(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                key: strip_allowed(child)
                for key, child in item.items()
                if str(key) not in {_ALLOWED_WEEKLY_KEY, _ALLOWED_WEEKLY_COMPLETENESS_KEY}
            }
        if isinstance(item, (list, tuple)):
            return [strip_allowed(child) for child in item]
        return item

    if isinstance(value, Mapping):
        labels = value.get("classification")
        if not isinstance(labels, (list, tuple, set)) or not all(label in labels for label in _PRESTART_LABELS):
            raise M14Error("M1.4 artifact is missing required pre-start classification labels")
    assert_m13_prestart_state_schema(strip_allowed(value), path)

    def walk(item: Any, item_path: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
                child_path = f"{item_path}.{key}" if item_path else str(key)
                if key_text in _FORBIDDEN_M14_KEYS:
                    raise M14Error(f"M1.4 artifact contains forbidden aggregate key: {child_path}")
                if key_text in {_ALLOWED_WEEKLY_KEY, _ALLOWED_WEEKLY_COMPLETENESS_KEY}:
                    continue
                walk(child, child_path)
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                walk(child, f"{item_path}[{index}]")

    walk(value)


def validate_extension_window(
    *, data_start: date, data_end: date, cutoff: datetime = PRESTART_CUTOFF
) -> None:
    """Reject any bridge request that could become a historical replay."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise M14DataError("pre-start cutoff must be timezone-aware")
    if data_start < EXTENSION_START:
        raise M14DataError("M1.4 fail-closed: data_start precedes 2026-09-01")
    if data_end > EXTENSION_END or data_end >= cutoff.date():
        raise M14DataError("M1.4 data_end includes incomplete or post-cutoff data")
    if data_end < data_start:
        raise M14DataError("M1.4 data_end precedes data_start")


def assert_no_historical_full_replay_invoked(
    *, data_start: date, historical_full_replay_invoked: bool = False
) -> None:
    validate_extension_window(data_start=data_start, data_end=EXTENSION_END)
    if historical_full_replay_invoked:
        raise M14DataError("M1.4 fail-closed: historical_full_replay_invoked is true")


def _write_json(path: Path, value: Mapping[str, Any]) -> str:
    assert_prestart_state_schema(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(_canonical(value), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return _file_sha256(path)


def verify_frozen_identity() -> dict[str, str]:
    try:
        actual = {
            "v1_control_sha256": strategy_spec_hash(),
            "v1_shadow_sha256": resolve_rules(variant="shadow").spec_hash,
            "v1_protocol_sha256": verify_protocol_hash(),
            "v2_1_spec_sha256": verify_v2_1_spec_hash(),
            "v2_1_protocol_sha256": verify_protocol_hash_from_v2_1(),
            "forward_anchor_sha256": verify_v2_1_forward_anchor_hash(),
        }
        validate_v2_1_forward_anchor()
    except Exception as exc:  # noqa: BLE001 - identity must fail closed
        raise M14IdentityError(f"frozen identity verification failed: {exc}") from exc
    expected = {
        "v1_control_sha256": V1_CONTROL_SHA256,
        "v1_shadow_sha256": V1_SHADOW_SHA256,
        "v1_protocol_sha256": M1_APPROVED_PROTOCOL_SHA256,
        "v2_1_spec_sha256": V2_1_SPEC_SHA256,
        "v2_1_protocol_sha256": V2_1_PROTOCOL_SHA256,
        "forward_anchor_sha256": FORWARD_ANCHOR_SHA256,
    }
    if actual != expected or V21_FORWARD_ANCHOR_STATUS != "FROZEN_NOT_YET_STARTED":
        raise M14IdentityError(f"frozen identity changed: expected={expected}, actual={actual}")
    return actual


def verify_protocol_hash_from_v2_1() -> str:
    """Import the V2.1 protocol verifier lazily to keep test helpers small."""
    from .v2_1_protocol import verify_v2_1_protocol_hash

    return verify_v2_1_protocol_hash()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise M14IdentityError(f"cannot load accepted artifact: {path}") from exc
    if not isinstance(value, dict):
        raise M14IdentityError(f"accepted artifact is not a mapping: {path}")
    return value


def _terminal_signal(histories: Sequence[SymbolHistory], signal_day: date) -> Any:
    """Compute only the final accepted Control signal, never a historical schedule."""
    validate_extension_window(data_start=EXTENSION_START, data_end=EXTENSION_END)
    if signal_day != date(2026, 8, 27):
        raise M14DataError("M1.4 terminal signal window is not the accepted 2026-08-27 PIT window")
    rules = resolve_rules(variant="control")
    bar_maps = {history.symbol: history.bars_by_day() for history in histories}
    universe = _build_universe_fast(histories, bar_maps, signal_day, rules)
    signal = _signal_from_universe(universe, rules)
    if signal is None:
        raise M14DataError("accepted terminal Control signal could not be reconstructed")
    return signal


def _direction_label(direction: int) -> str:
    return "LONG" if int(direction) > 0 else "SHORT"


def _m13_overlay(histories: Sequence[SymbolHistory]) -> Any:
    overlay = DEFAULT_OVERLAY.with_terminal_closes(histories)
    overlay_hash = _sha256_json(overlay.as_dict())
    if overlay_hash != M13_OVERLAY_SHA256:
        raise M14IdentityError(
            f"accepted M1.3 lifecycle overlay hash changed: {overlay_hash}"
        )
    return overlay


def build_m13_checkpoint(
    *, dataset: Mapping[str, Any], write: bool = False
) -> tuple[dict[str, Any], str, tuple[SymbolHistory, ...]]:
    """Build the opaque carry-state checkpoint from accepted M1.3 artifacts."""
    if dataset.get("dataset_sha256") != EXPECTED_DATASET_SHA256:
        raise M14IdentityError("frozen dataset SHA changed")
    if dataset.get("normalized_dataset_sha256") != EXPECTED_NORMALIZED_DATASET_SHA256:
        raise M14IdentityError("frozen normalized dataset SHA changed")
    manifest = dataset.get("manifest")
    if not isinstance(manifest, Mapping) or manifest.get("last_available_date") != FROZEN_DATA_END.isoformat():
        raise M14IdentityError("frozen dataset boundary is not 2026-08-31")
    before = snapshot_m13_artifacts()
    if before != M13_ARTIFACT_SHA256:
        raise M14IdentityError(f"M1.3 artifacts changed before checkpoint: {before}")

    parent = _load_json(M13_DIR / "V2_1_M1_3_PARENT_RECONSTRUCTION.json")
    holding = _load_json(M13_DIR / "HOLDING_PROVENANCE.json")
    if parent.get("decision") != "READY":
        raise M14IdentityError("M1.3 accepted parent is not READY")
    if parent.get("code_commit_used_by_run") != M13_CODE_COMMIT:
        raise M14IdentityError("M1.3 code commit binding changed")
    if parent.get("identity", {}).get("control_sha256") != V1_CONTROL_SHA256:
        raise M14IdentityError("M1.3 Control identity binding changed")

    raw_histories = tuple(dataset["usable_histories"])
    overlay = _m13_overlay(raw_histories)
    histories = apply_lifecycle_overlay(raw_histories, overlay)
    signal = _terminal_signal(histories, date(2026, 8, 27))

    terminal_rows = [
        row
        for row in holding.get("records", [])
        if row.get("accounting_exit_timestamp_utc") == CHECKPOINT_TIMESTAMP
    ]
    if len(terminal_rows) != len(signal.targets):
        raise M14DataError("M1.3 terminal holding count does not match the accepted Control target")
    entry_by_symbol = {str(row["symbol"]): row for row in terminal_rows}
    if set(entry_by_symbol) != set(signal.targets):
        raise M14DataError("M1.3 terminal symbols do not match the accepted Control target")

    position_rows: list[dict[str, Any]] = []
    by_symbol = {history.symbol: history for history in histories}
    for symbol, direction in sorted(signal.targets.items()):
        history = by_symbol[symbol]
        bar = next((item for item in history.daily_bars if item.day == FROZEN_DATA_END), None)
        if bar is None or not math.isfinite(bar.close) or bar.close <= 0:
            raise M14DataError(f"missing terminal frozen mark for {symbol}")
        entry = entry_by_symbol[symbol]
        position_rows.append(
            {
                "symbol": symbol,
                "direction": _direction_label(direction),
                "direction_code": int(direction),
                "target_notional": 1.0 / float(len(signal.targets)),
                "entry_timestamp_utc": entry["accounting_entry_timestamp_utc"],
                "last_mark_timestamp_utc": _iso_ms(bar.close_time_ms),
                "last_mark_price": float(bar.close),
            }
        )
    if any(row["last_mark_timestamp_utc"] != CHECKPOINT_TIMESTAMP for row in position_rows):
        raise M14DataError("terminal marks are not all at the accepted 2026-08-31 close")

    last_execution = max(
        date.fromisoformat(row["accounting_entry_timestamp_utc"][:10])
        for row in terminal_rows
    )
    if last_execution != date(2026, 8, 28):
        raise M14DataError("accepted M1.3 terminal scheduler clock is not 2026-08-28")
    last_signal = last_execution - timedelta(days=1)
    scheduler = {
        "last_successful_rebalance_execution_day": last_execution.isoformat(),
        "last_successful_signal_day": last_signal.isoformat(),
        "rebalance_interval_days": 7,
        "retry_rule": "FAILED_DUE_DAY_RETRIES_ON_NEXT_CALENDAR_DAY",
        "next_rebalance_due_day": (last_execution + timedelta(days=7)).isoformat(),
        "pending_retry": False,
        "scheduler_initialized_from_checkpoint": True,
    }

    m13_accounting = parent.get("accounting", {})
    required_internal = (
        "price_component_total",
        "funding_component_total",
        "transaction_cost_component_total",
    )
    try:
        historical_end_equity = 1.0 + sum(float(m13_accounting[key]) for key in required_internal)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise M14IdentityError("accepted M1.3 accounting carry state is incomplete") from exc
    if not math.isfinite(historical_end_equity) or historical_end_equity <= 0:
        raise M14IdentityError("accepted M1.3 accounting carry state is invalid")
    opaque_equity_sha = _sha256_json({"terminal_equity": historical_end_equity, "source": M13_RESULT_COMMIT})
    continuity_payload = {
        "as_of": CHECKPOINT_TIMESTAMP,
        "positions": position_rows,
        "scheduler": scheduler,
        "opaque_equity_sha256": opaque_equity_sha,
        "dataset_sha256": dataset["dataset_sha256"],
        "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
        "overlay_sha256": M13_OVERLAY_SHA256,
    }
    checkpoint: dict[str, Any] = {
        "schema_version": "M1_3_ACCEPTED_STATE_CHECKPOINT.v1",
        "classification": list(_PRESTART_LABELS),
        "scope": "STATE_CARRY_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "as_of_utc": CHECKPOINT_TIMESTAMP,
        "m1_3_result_commit": M13_RESULT_COMMIT,
        "m1_3_code_commit": M13_CODE_COMMIT,
        "m1_3_overlay_sha256": M13_OVERLAY_SHA256,
        "dataset_sha256": dataset["dataset_sha256"],
        "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
        "last_successful_rebalance_execution_day": last_execution.isoformat(),
        "last_successful_signal_day": last_signal.isoformat(),
        "current_control_target_symbols": [row["symbol"] for row in position_rows],
        "current_control_positions": position_rows,
        "scheduler_clock_state": scheduler,
        "accounting_state_continuity_hash": _sha256_json(continuity_payload),
        "historical_equity_state": {
            "classification": "STATE_CARRY_ONLY",
            "numeric_value_exposed": False,
            "opaque_sha256": opaque_equity_sha,
        },
        "historical_full_replay_invoked": False,
        "historical_result_output": "QUARANTINED_NOT_EMITTED",
    }
    assert_prestart_state_schema(checkpoint)
    if write:
        _write_json(M14_CHECKPOINT_PATH, checkpoint)
    return checkpoint, opaque_equity_sha, histories


def _request_url(endpoint: str, params: Mapping[str, Any]) -> str:
    if not params:
        return endpoint
    return f"{endpoint}?{urlencode(sorted((str(k), str(v)) for k, v in params.items()))}"


def _safe_raw_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise M14DataError(f"unsafe pre-start raw filename: {name}")
    return name


def _fetch_json(
    *, name: str, endpoint: str, params: Mapping[str, Any], raw_name: str
) -> tuple[Any | None, RequestRecord]:
    request_url = _request_url(endpoint, params)
    requested_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    content = b""
    status: int | None = None
    error: str | None = None
    try:
        request = Request(request_url, method="GET")
        with urlopen(request, timeout=60) as response:
            status = int(response.getcode())
            content = bytes(response.read() or b"")
    except HTTPError as exc:
        status = int(exc.code)
        try:
            content = bytes(exc.read() or b"")
        except OSError:
            content = b""
        error = f"HTTP {status}"
    except (OSError, URLError, TimeoutError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    response_hash = _sha256_bytes(content) if content else None
    local_path: str | None = None
    if content:
        target = M14_RAW_DIR / _safe_raw_name(raw_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        local_path = target.relative_to(PROJECT_ROOT).as_posix()
    payload: Any | None = None
    if status == 200 and error is None:
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = f"invalid JSON: {type(exc).__name__}"
    elif status is not None and status >= 400:
        error = error or f"HTTP {status}"
    record = RequestRecord(
        name=name,
        method="GET",
        endpoint=endpoint,
        params=dict(params),
        request_url=request_url,
        request_timestamp_utc=requested_at,
        http_status=status,
        response_sha256=response_hash,
        local_path=local_path,
        row_count=0,
        first_timestamp=None,
        last_timestamp=None,
        error=error,
    )
    return payload, record


def _record_with_rows(
    record: RequestRecord, *, count: int, first: str | None, last: str | None, error: str | None = None
) -> RequestRecord:
    return replace(record, row_count=count, first_timestamp=first, last_timestamp=last, error=error or record.error)


def _parse_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise M14DataError(f"{field} is not an integer") from exc


def _parse_daily_payload(payload: Any, symbol: str) -> tuple[DailyBar, ...]:
    if not isinstance(payload, list):
        raise M14DataError(f"{symbol} daily response is not a list")
    result: list[DailyBar] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 8:
            raise M14DataError(f"{symbol} daily response row is malformed")
        open_ms = _parse_int(row[0], "open_time")
        close_ms = _parse_int(row[6], "close_time")
        day = datetime.fromtimestamp(open_ms / 1000.0, UTC).date()
        if not EXTENSION_START <= day <= EXTENSION_END or close_ms >= EXTENSION_END_EXCLUSIVE_MS:
            raise M14DataError(f"{symbol} daily response contains data outside the fixed window")
        try:
            values = [float(row[index]) for index in (1, 2, 3, 4, 7)]
        except (TypeError, ValueError, OverflowError, IndexError) as exc:
            raise M14DataError(f"{symbol} daily response contains nonnumeric data") from exc
        if close_ms < open_ms or values[3] <= 0 or any(not math.isfinite(value) for value in values):
            raise M14DataError(f"{symbol} daily response contains invalid completed bar")
        result.append(
            DailyBar(
                symbol=symbol,
                day=day,
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                quote_volume=values[4],
                open_time_ms=open_ms,
                close_time_ms=close_ms,
            )
        )
    if len({bar.day for bar in result}) != len(result):
        raise M14DataError(f"{symbol} daily response contains duplicate completed days")
    return tuple(sorted(result, key=lambda bar: (bar.day, bar.open_time_ms)))


def _parse_funding_payload(
    payload: Any,
    symbol: str,
    history: SymbolHistory | None,
    funding_info: Mapping[str, Any] | None,
) -> tuple[FundingEvent, ...]:
    if not isinstance(payload, list):
        raise M14DataError(f"{symbol} funding response is not a list")
    baseline: float | None = None
    if history is not None and history.funding_events:
        baseline = float(history.funding_events[-1].funding_interval_hours)
    if baseline is None and funding_info is not None:
        try:
            baseline = float(funding_info.get("fundingIntervalHours"))
        except (TypeError, ValueError, OverflowError):
            baseline = None
    if baseline is None or not math.isfinite(baseline) or baseline <= 0:
        raise M14DataError(f"{symbol} has no valid frozen or official funding interval")
    events: list[FundingEvent] = []
    for row in payload:
        if not isinstance(row, Mapping):
            raise M14DataError(f"{symbol} funding response row is malformed")
        event_ms = _parse_int(row.get("fundingTime"), "fundingTime")
        if not EXTENSION_START_MS <= event_ms < EXTENSION_END_EXCLUSIVE_MS:
            raise M14DataError(f"{symbol} funding response contains data outside the fixed window")
        try:
            rate = float(row.get("fundingRate"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise M14DataError(f"{symbol} funding rate is not numeric") from exc
        if not math.isfinite(rate):
            raise M14DataError(f"{symbol} funding rate is not finite")
        events.append(FundingEvent(symbol, event_ms, rate, baseline))
    if len({event.funding_time_ms for event in events}) != len(events):
        raise M14DataError(f"{symbol} funding response contains duplicate settlements")
    return tuple(sorted(events, key=lambda event: event.funding_time_ms))


def _parse_exchange_info(payload: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("symbols"), list):
        raise M14DataError("exchangeInfo response has no symbols list")
    result: dict[str, Mapping[str, Any]] = {}
    for item in payload["symbols"]:
        if not isinstance(item, Mapping) or not item.get("symbol"):
            continue
        symbol = str(item["symbol"]).upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            continue
        result[symbol] = {
            key: item[key]
            for key in (
                "symbol",
                "status",
                "contractType",
                "quoteAsset",
                "baseAsset",
                "onboardDate",
                "deliveryDate",
            )
            if key in item
        }
    return result


def _parse_funding_info(payload: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(payload, list):
        raise M14DataError("fundingInfo response is not a list")
    return {
        str(item["symbol"]).upper(): dict(item)
        for item in payload
        if isinstance(item, Mapping) and item.get("symbol")
    }


def candidate_has_pit_proof(
    *, onboard_timestamp_ms: int | None, first_completed_bar: DailyBar | None
) -> bool:
    """Current exchangeInfo alone is insufficient; a real completed bar is required."""
    return (
        onboard_timestamp_ms is not None
        and first_completed_bar is not None
        and onboard_timestamp_ms < first_completed_bar.close_time_ms
        and first_completed_bar.day >= EXTENSION_START
        and first_completed_bar.day <= EXTENSION_END
    )


def _fetch_symbol_extension(
    *, symbol: str, history: SymbolHistory | None, funding_info: Mapping[str, Any] | None
) -> ExtensionSymbolResult:
    daily_params = {
        "symbol": symbol,
        "interval": "1d",
        "startTime": str(EXTENSION_START_MS),
        "endTime": str(LAST_ELIGIBLE_MS),
        "limit": "1000",
    }
    daily_payload, daily_record = _fetch_json(
        name=f"{symbol}.daily_1d",
        endpoint=KLINES_ENDPOINT,
        params=daily_params,
        raw_name=f"daily_{symbol}.json",
    )
    daily: tuple[DailyBar, ...] = ()
    if daily_payload is not None and daily_record.error is None:
        try:
            daily = _parse_daily_payload(daily_payload, symbol)
            daily_record = _record_with_rows(
                daily_record,
                count=len(daily),
                first=_iso_ms(daily[0].open_time_ms) if daily else None,
                last=_iso_ms(daily[-1].close_time_ms) if daily else None,
            )
        except M14DataError as exc:
            daily_record = replace(daily_record, error=str(exc))

    funding_params = {
        "symbol": symbol,
        "startTime": str(EXTENSION_START_MS),
        "endTime": str(LAST_ELIGIBLE_MS),
        "limit": "1000",
    }
    funding_payload, funding_record = _fetch_json(
        name=f"{symbol}.funding_rate",
        endpoint=FUNDING_RATE_ENDPOINT,
        params=funding_params,
        raw_name=f"funding_{symbol}.json",
    )
    funding: tuple[FundingEvent, ...] = ()
    if funding_payload is not None and funding_record.error is None:
        try:
            funding = _parse_funding_payload(funding_payload, symbol, history, funding_info)
            funding_record = _record_with_rows(
                funding_record,
                count=len(funding),
                first=_iso_ms(funding[0].funding_time_ms) if funding else None,
                last=_iso_ms(funding[-1].funding_time_ms) if funding else None,
            )
        except M14DataError as exc:
            funding_record = replace(funding_record, error=str(exc))
    expected_days: set[date] = set()
    if history is not None:
        expected_days = set(_day_range(EXTENSION_START, EXTENSION_END))
    elif daily:
        expected_days = set(_day_range(daily[0].day, EXTENSION_END))
    missing = tuple(sorted(expected_days - {bar.day for bar in daily}))
    return ExtensionSymbolResult(
        symbol=symbol,
        daily_bars=daily,
        funding_events=funding,
        daily_request=daily_record,
        funding_request=funding_record,
        exchange_info=None,
        missing_daily_days=missing,
    )


def _request_to_dict(record: RequestRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "method": record.method,
        "endpoint": record.endpoint,
        "params": dict(record.params),
        "request_url": record.request_url,
        "request_timestamp_utc": record.request_timestamp_utc,
        "http_status": record.http_status,
        "response_sha256": record.response_sha256,
        "local_path": record.local_path,
        "row_count": record.row_count,
        "first_timestamp": record.first_timestamp,
        "last_timestamp": record.last_timestamp,
        "error": record.error,
    }


def _new_listing_record(
    *, symbol: str, info: Mapping[str, Any], first_bar: DailyBar, exchange_response_sha256: str | None
) -> dict[str, Any]:
    onboard_ms = int(info["onboardDate"])
    onboard_iso = _iso_ms(onboard_ms)
    source_hash = _sha256_json(
        {
            "symbol": symbol,
            "onboardDate": onboard_ms,
            "exchangeInfoResponseSha256": exchange_response_sha256,
            "first_completed_bar": first_bar.day.isoformat(),
        }
    )
    return {
        "symbol": symbol,
        "source": EXCHANGE_INFO_ENDPOINT,
        "announcement_publication_timestamp_utc": onboard_iso,
        "effective_timestamp_utc": onboard_iso,
        "source_hash": source_hash,
        "classification": "NEW_LISTING_PIT_CONFIRMED_BY_ONBOARD_AND_COMPLETED_BAR",
        "pit_evidence": {
            "exchange_info_onboard_timestamp_utc": onboard_iso,
            "first_completed_daily_bar_day": first_bar.day.isoformat(),
            "first_completed_daily_bar_close_timestamp_utc": _iso_ms(first_bar.close_time_ms),
            "current_exchange_info_alone_not_used_as_pit_proof": True,
        },
    }


def fetch_prestart_extension(
    *, histories: Sequence[SymbolHistory], write_raw: bool = True
) -> tuple[tuple[SymbolHistory, ...], dict[str, Any], dict[str, Any], dict[str, ExtensionSymbolResult]]:
    """Fetch only the fixed pre-start extension from Binance GET endpoints."""
    validate_extension_window(data_start=EXTENSION_START, data_end=EXTENSION_END)
    if write_raw:
        M14_RAW_DIR.mkdir(parents=True, exist_ok=True)
    exchange_payload, exchange_record = _fetch_json(
        name="exchange_info",
        endpoint=EXCHANGE_INFO_ENDPOINT,
        params={},
        raw_name="exchangeInfo.json",
    )
    funding_info_payload, funding_info_record = _fetch_json(
        name="funding_info",
        endpoint=FUNDING_INFO_ENDPOINT,
        params={},
        raw_name="fundingInfo.json",
    )
    if exchange_payload is None or exchange_record.error is not None:
        raise M14DataError(f"official exchangeInfo GET failed: {exchange_record.error}")
    if funding_info_payload is None or funding_info_record.error is not None:
        raise M14DataError(f"official fundingInfo GET failed: {funding_info_record.error}")
    exchange_info = _parse_exchange_info(exchange_payload)
    funding_info = _parse_funding_info(funding_info_payload)
    exchange_record = _record_with_rows(exchange_record, count=len(exchange_info), first=None, last=None)
    funding_info_record = _record_with_rows(funding_info_record, count=len(funding_info), first=None, last=None)

    frozen_by_symbol = {history.symbol.upper(): history for history in histories}
    active_frozen = [
        history
        for history in histories
        if history.active_on(EXTENSION_START) and history.delisted_at is None
    ]
    candidates: dict[str, Mapping[str, Any]] = {}
    ambiguous_candidates: list[dict[str, Any]] = []
    for symbol, info in sorted(exchange_info.items()):
        if symbol in frozen_by_symbol:
            continue
        onboard_raw = info.get("onboardDate")
        try:
            onboard_ms = int(onboard_raw) if onboard_raw is not None else None
        except (TypeError, ValueError, OverflowError):
            onboard_ms = None
        onboard_day = (
            datetime.fromtimestamp(onboard_ms / 1000.0, UTC).date()
            if onboard_ms is not None and onboard_ms > 0
            else None
        )
        if onboard_day is not None and EXTENSION_START <= onboard_day <= EXTENSION_END:
            candidates[symbol] = info
        elif onboard_day is not None and onboard_day < EXTENSION_START:
            ambiguous_candidates.append(
                {
                    "symbol": symbol,
                    "reason": "CURRENT_EXCHANGE_INFO_ONLY_CANNOT_PROVE_PAST_PIT_MEMBERSHIP",
                    "onboard_day": onboard_day.isoformat(),
                }
            )

    fetch_specs: list[tuple[str, SymbolHistory | None]] = [
        (history.symbol, history) for history in sorted(active_frozen, key=lambda item: item.symbol)
    ]
    fetch_specs.extend((symbol, None) for symbol in sorted(candidates))
    results: dict[str, ExtensionSymbolResult] = {}
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="v21-m14-get") as pool:
        futures = {
            pool.submit(
                _fetch_symbol_extension,
                symbol=symbol,
                history=history,
                funding_info=funding_info.get(symbol),
            ): symbol
            for symbol, history in fetch_specs
        }
        for future in as_completed(futures):
            result = future.result()
            info = exchange_info.get(result.symbol)
            results[result.symbol] = replace(result, exchange_info=info)

    requests = [exchange_record, funding_info_record]
    for result in results.values():
        requests.extend((result.daily_request, result.funding_request))
    request_rows = [_request_to_dict(record) for record in sorted(requests, key=lambda item: item.name)]

    missing_daily: dict[str, list[str]] = {}
    funding_request_failures: list[str] = []
    merged: list[SymbolHistory] = []
    for history in sorted(histories, key=lambda item: item.symbol):
        result = results.get(history.symbol)
        if result is None:
            merged.append(history)
            continue
        if result.missing_daily_days:
            missing_daily[history.symbol] = [day.isoformat() for day in result.missing_daily_days]
        if result.funding_request.error is not None:
            funding_request_failures.append(history.symbol)
        existing_days = {bar.day for bar in history.daily_bars}
        if existing_days.intersection(bar.day for bar in result.daily_bars):
            raise M14DataError(f"{history.symbol} extension overlaps frozen daily data")
        existing_funding = {event.funding_time_ms for event in history.funding_events}
        if existing_funding.intersection(event.funding_time_ms for event in result.funding_events):
            raise M14DataError(f"{history.symbol} extension overlaps frozen funding data")
        merged.append(
            replace(
                history,
                daily_bars=tuple(sorted((*history.daily_bars, *result.daily_bars), key=lambda bar: (bar.day, bar.open_time_ms))),
                funding_events=tuple(sorted((*history.funding_events, *result.funding_events), key=lambda event: event.funding_time_ms)),
            )
        )

    lifecycle_records: list[dict[str, Any]] = []
    exchange_sha = exchange_record.response_sha256
    for symbol, info in sorted(candidates.items()):
        result = results[symbol]
        if not result.daily_bars:
            ambiguous_candidates.append(
                {
                    "symbol": symbol,
                    "reason": "NEW_LISTING_HAS_NO_COMPLETED_BAR_IN_WINDOW",
                }
            )
            continue
        onboard_ms = int(info["onboardDate"])
        if not candidate_has_pit_proof(onboard_timestamp_ms=onboard_ms, first_completed_bar=result.daily_bars[0]):
            ambiguous_candidates.append(
                {
                    "symbol": symbol,
                    "reason": "CURRENT_EXCHANGE_INFO_LACKS_PIT_MEMBERSHIP_PROOF",
                }
            )
            continue
        first_bar = result.daily_bars[0]
        onboard_day = datetime.fromtimestamp(onboard_ms / 1000.0, UTC).date()
        lifecycle = LifecycleRecord(
            symbol=symbol,
            first_available_day=first_bar.day,
            last_available_day=result.daily_bars[-1].day,
            listed_from=max(onboard_day, first_bar.day),
            delisted_at=None,
            currently_active=True,
            lifecycle_source="official_exchangeInfo_onboardDate_plus_completed_daily_bar",
            lifecycle_confidence="official_pit_confirmed",
            status="OK",
        )
        merged.append(
            SymbolHistory(
                symbol=symbol,
                daily_bars=result.daily_bars,
                funding_events=result.funding_events,
                lifecycle=lifecycle,
            )
        )
        lifecycle_records.append(
            _new_listing_record(
                symbol=symbol,
                info=info,
                first_bar=first_bar,
                exchange_response_sha256=exchange_sha,
            )
        )

    if missing_daily:
        # Do not delete a symbol or fill a bar.  The gate consumes this list
        # and produces NOT_READY with the exact provenance preserved.
        pass
    extension_lifecycle: dict[str, Any] = {
        "schema_version": "PRESTART_LIFECYCLE_EXTENSION.v1",
        "classification": list(_PRESTART_LABELS),
        "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
        "extension_start_utc": datetime.combine(EXTENSION_START, time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        "extension_end_utc": PRESTART_CUTOFF.isoformat().replace("+00:00", "Z"),
        "records": lifecycle_records,
        "ambiguous_candidates": ambiguous_candidates,
        "no_fabricated_lifecycle": True,
    }
    manifest: dict[str, Any] = {
        "schema_version": "PRESTART_EXTENSION_MANIFEST.v1",
        "classification": list(_PRESTART_LABELS),
        "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
        "venue": "Binance USD-M Futures",
        "http_method_policy": "GET_ONLY",
        "official_endpoints": [EXCHANGE_INFO_ENDPOINT, FUNDING_INFO_ENDPOINT, KLINES_ENDPOINT, FUNDING_RATE_ENDPOINT],
        "request_window": {
            "extension_start_utc": datetime.combine(EXTENSION_START, time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
            "extension_end_utc_exclusive": PRESTART_CUTOFF.isoformat().replace("+00:00", "Z"),
            "last_eligible_timestamp_utc": _iso_ms(LAST_ELIGIBLE_MS),
            "completed_daily_closes_only": True,
        },
        "frozen_parent": {
            "dataset_sha256": EXPECTED_DATASET_SHA256,
            "normalized_dataset_sha256": EXPECTED_NORMALIZED_DATASET_SHA256,
            "m1_3_overlay_sha256": M13_OVERLAY_SHA256,
            "historical_cache_rewritten": False,
        },
        "candidate_discovery": {
            "current_exchange_info_used_only_for_candidate_discovery": True,
            "pit_membership_requires_timestamped_onboard_and_completed_bar": True,
            "new_listing_candidate_count": len(candidates),
            "ambiguous_candidate_count": len(ambiguous_candidates),
            "candidate_symbols": sorted(candidates),
        },
        "coverage": {
            "frozen_active_symbol_count": len(active_frozen),
            "symbols_fetched": sorted(results),
            "missing_daily_by_symbol": missing_daily,
            "funding_request_failure_symbols": sorted(funding_request_failures),
            "synthetic_settlements": False,
            "zero_fill_or_interpolation": False,
        },
        "requests": request_rows,
        "fetch_summary": {
            "request_count": len(request_rows),
            "successful_http_200_count": sum(row["http_status"] == 200 for row in request_rows),
            "response_hash_count": sum(row["response_sha256"] is not None for row in request_rows),
            "official_get_only": all(row["method"] == "GET" for row in request_rows),
        },
    }
    assert_prestart_state_schema(manifest)
    assert_prestart_state_schema(extension_lifecycle)
    return tuple(sorted(merged, key=lambda item: item.symbol)), manifest, extension_lifecycle, results


def _position_from_checkpoint(row: Mapping[str, Any]) -> _BridgePosition:
    try:
        direction = int(row["direction_code"])
        notional = float(row["target_notional"])
        entry_ms = int(datetime.fromisoformat(str(row["entry_timestamp_utc"]).replace("Z", "+00:00")).timestamp() * 1000)
        mark_ms = int(datetime.fromisoformat(str(row["last_mark_timestamp_utc"]).replace("Z", "+00:00")).timestamp() * 1000)
        mark_price = float(row["last_mark_price"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise M14DataError("checkpoint position is malformed") from exc
    if direction not in (-1, 1) or notional <= 0 or mark_ms <= 0 or not math.isfinite(mark_price) or mark_price <= 0:
        raise M14DataError("checkpoint position contains invalid state")
    return _BridgePosition(str(row["symbol"]), direction, notional, entry_ms, mark_ms, mark_price)


def carry_scheduler_state(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    scheduler = checkpoint.get("scheduler_clock_state")
    if not isinstance(scheduler, Mapping):
        raise M14DataError("checkpoint scheduler clock is missing")
    if scheduler.get("scheduler_initialized_from_checkpoint") is not True:
        raise M14DataError("scheduler was not carried from the accepted checkpoint")
    if scheduler.get("rebalance_interval_days") != 7:
        raise M14DataError("frozen seven-day rebalance clock changed")
    due = date.fromisoformat(str(scheduler["next_rebalance_due_day"]))
    last = date.fromisoformat(str(scheduler["last_successful_rebalance_execution_day"]))
    if due != last + timedelta(days=7):
        raise M14DataError("checkpoint scheduler due day is inconsistent")
    return dict(scheduler)


def carry_positions_without_reopen(checkpoint: Mapping[str, Any]) -> dict[str, _BridgePosition]:
    rows = checkpoint.get("current_control_positions")
    if not isinstance(rows, list) or not rows:
        raise M14DataError("checkpoint has no current parent positions")
    positions = {_position_from_checkpoint(row).symbol: _position_from_checkpoint(row) for row in rows}
    if len(positions) != len(rows):
        raise M14DataError("checkpoint contains duplicate positions")
    if any(position.entry_timestamp_ms >= position.mark_timestamp_ms for position in positions.values()):
        raise M14DataError("checkpoint position entry/mark continuity is invalid")
    return positions


def _unique_funding(history: SymbolHistory) -> tuple[FundingEvent, ...]:
    by_time: dict[int, FundingEvent] = {}
    for event in history.funding_events:
        by_time.setdefault(event.funding_time_ms, event)
    return tuple(sorted(by_time.values(), key=lambda event: event.funding_time_ms))


def _boundary_delta(
    *, positions: Mapping[str, _BridgePosition], histories: Mapping[str, SymbolHistory]
) -> tuple[float, bool, list[str]]:
    """Derive only the 2026-08-31 boundary needed for weekly continuity."""
    total = 0.0
    issues: list[str] = []
    for symbol, position in sorted(positions.items()):
        history = histories.get(symbol)
        if history is None:
            issues.append(f"missing checkpoint history {symbol}")
            continue
        bars = history.bars_by_day()
        previous = bars.get(date(2026, 8, 30))
        terminal = bars.get(FROZEN_DATA_END)
        if previous is None or terminal is None:
            issues.append(f"missing checkpoint boundary bar {symbol}")
            continue
        events = _unique_funding(history)
        times = tuple(event.funding_time_ms for event in events)
        settled = events[bisect_right(times, previous.close_time_ms):bisect_right(times, terminal.close_time_ms)]
        if not settled:
            issues.append(f"missing checkpoint boundary funding {symbol}")
            continue
        total += position.direction * position.notional * (terminal.close / previous.close - 1.0)
        total += sum(-position.direction * position.notional * event.funding_rate for event in settled)
    return total, not issues, issues


def _apply_mark(
    *, day: date, positions: dict[str, _BridgePosition], histories: Mapping[str, SymbolHistory]
) -> tuple[float, bool, list[str], int]:
    day_delta = 0.0
    issues: list[str] = []
    funding_issue_count = 0
    for symbol, position in sorted(tuple(positions.items())):
        history = histories.get(symbol)
        if history is None:
            issues.append(f"missing held history {symbol}")
            continue
        bars = history.bars_by_day()
        bar = bars.get(day)
        if bar is None:
            if history.lifecycle.delisted_at is not None and day >= history.lifecycle.delisted_at:
                terminal = history.last_bar_on_or_before(history.lifecycle.delisted_at - timedelta(days=1))
                if terminal is None or terminal.close <= 0:
                    issues.append(f"missing lifecycle terminal close {symbol}")
                    continue
                positions.pop(symbol, None)
                continue
            issues.append(f"missing completed mark {symbol} {day.isoformat()}")
            continue
        previous = bars.get(day - timedelta(days=1))
        if previous is None:
            issues.append(f"non-consecutive mark {symbol} {day.isoformat()}")
            continue
        events = _unique_funding(history)
        times = tuple(event.funding_time_ms for event in events)
        settled = events[bisect_right(times, position.mark_timestamp_ms):bisect_right(times, bar.close_time_ms)]
        if not settled:
            issue = f"missing required funding coverage {symbol} {day.isoformat()}"
            issues.append(issue)
            funding_issue_count += 1
            continue
        if not math.isfinite(position.mark_price) or position.mark_price <= 0:
            issues.append(f"invalid carried mark price {symbol}")
            continue
        day_delta += position.direction * position.notional * (bar.close / position.mark_price - 1.0)
        day_delta += sum(-position.direction * position.notional * event.funding_rate for event in settled)
        position.mark_timestamp_ms = bar.close_time_ms
        position.mark_price = float(bar.close)
    return day_delta, not issues, issues, funding_issue_count


def _transition_positions(
    *,
    day: date,
    signal: Any,
    positions: dict[str, _BridgePosition],
    histories: Mapping[str, SymbolHistory],
) -> tuple[float, int, int, list[str]]:
    rules = resolve_rules(variant="control")
    target_notional = 1.0 / float(rules.k_long + rules.k_short)
    targets = signal.targets
    bar_maps = {symbol: history.bars_by_day() for symbol, history in histories.items()}
    affected = {
        symbol
        for symbol in set(positions) | set(targets)
        if symbol not in targets
        or symbol not in positions
        or positions[symbol].direction != targets[symbol]
    }
    missing = [symbol for symbol in sorted(affected) if symbol in targets and day not in bar_maps.get(symbol, {})]
    if missing:
        return 0.0, 0, 0, [f"missing execution price {symbol} {day.isoformat()}" for symbol in missing]
    cost_rate = (rules.taker_fee_pct + rules.slippage_pct) / 100.0
    delta = 0.0
    transaction_count = 0
    closed_interval_count = 0
    for symbol, old in sorted(tuple(positions.items())):
        target_direction = targets.get(symbol)
        same = target_direction == old.direction and math.isclose(old.notional, target_notional, rel_tol=0.0, abs_tol=1e-15)
        if same:
            continue
        bar = bar_maps[symbol].get(day)
        if bar is None:
            return delta, transaction_count, closed_interval_count, [f"missing resize close {symbol} {day.isoformat()}"]
        delta -= old.notional * cost_rate
        transaction_count += 1
        closed_interval_count += 1
        if target_direction == old.direction:
            delta -= abs(target_notional - old.notional) * cost_rate
            positions[symbol] = _BridgePosition(
                symbol=symbol,
                direction=old.direction,
                notional=target_notional,
                entry_timestamp_ms=bar.close_time_ms,
                mark_timestamp_ms=bar.close_time_ms,
                mark_price=float(bar.close),
            )
        else:
            positions.pop(symbol, None)
    for symbol, direction in sorted(targets.items()):
        if symbol in positions:
            continue
        bar = bar_maps[symbol].get(day)
        if bar is None:
            return delta, transaction_count, closed_interval_count, [f"missing entry close {symbol} {day.isoformat()}"]
        positions[symbol] = _BridgePosition(
            symbol=symbol,
            direction=int(direction),
            notional=target_notional,
            entry_timestamp_ms=bar.close_time_ms,
            mark_timestamp_ms=bar.close_time_ms,
            mark_price=float(bar.close),
        )
        delta -= target_notional * cost_rate
        transaction_count += 1
    return delta, transaction_count, closed_interval_count, []


def compute_weekly_return(*, prior_equity: float, week_end_equity: float) -> float:
    if not math.isfinite(prior_equity) or not math.isfinite(week_end_equity) or prior_equity <= 0:
        raise M14DataError("weekly equity continuity is invalid")
    return week_end_equity / prior_equity - 1.0


def select_latest_current_weeks(
    rows: Sequence[Mapping[str, Any]], *, cutoff: datetime = PRESTART_CUTOFF, lookback: int = 13
) -> tuple[dict[str, Any], ...]:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise M14DataError("weekly cutoff must be timezone-aware")
    parsed: list[dict[str, Any]] = []
    seen: set[date] = set()
    for raw in rows:
        try:
            week_start = date.fromisoformat(str(raw["week_start"]))
            week_end = date.fromisoformat(str(raw["week_end"]))
            completed_at = datetime.fromisoformat(str(raw["completed_at_utc"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise M14DataError("weekly provenance row is malformed") from exc
        completed_at = completed_at.astimezone(UTC)
        if week_end in seen:
            raise M14DataError(f"duplicate weekly period {week_end.isoformat()}")
        seen.add(week_end)
        if week_end - week_start != timedelta(days=6) or week_start.weekday() != 0:
            raise M14DataError("weekly period is not a Monday-Sunday period")
        if completed_at >= cutoff:
            continue
        row = dict(raw)
        row["week_start"] = week_start.isoformat()
        row["week_end"] = week_end.isoformat()
        row["completed_at_utc"] = completed_at.isoformat().replace("+00:00", "Z")
        parsed.append(row)
    parsed.sort(key=lambda row: (row["completed_at_utc"], row["week_end"]))
    if len(parsed) < lookback:
        raise M14DataError(f"only {len(parsed)} complete weekly periods before cutoff")
    selected = tuple(parsed[-lookback:])
    if len({row["week_end"] for row in selected}) != lookback:
        raise M14DataError("selected weekly periods are not distinct")
    return selected


def build_risk_state(
    weekly_rows: Sequence[Mapping[str, Any]], *, cutoff: datetime = PRESTART_CUTOFF
) -> dict[str, Any]:
    selected = select_latest_current_weeks(weekly_rows, cutoff=cutoff)
    records = [
        ControlWeeklyReturn(
            week_ending=date.fromisoformat(str(row["week_end"])),
            net_return=float(row[_ALLOWED_WEEKLY_KEY]),
            completed_at=datetime.fromisoformat(str(row["completed_at_utc"]).replace("Z", "+00:00")),
            complete=row.get("complete") is True,
        )
        for row in selected
    ]
    result = evaluate_risk_scale(records, signal_time=cutoff)
    output: dict[str, Any] = {
        "schema_version": "CURRENT_LATEST_13_RISK_INPUT.v1",
        "classification": list(_PRESTART_LABELS),
        "scope": "RISK_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "cutoff_utc": cutoff.isoformat().replace("+00:00", "Z"),
        "selected_count": len(selected),
        "latest_week_endings": [row["week_end"] for row in selected],
        "rows": [
            {
                "week_start": row["week_start"],
                "week_end": row["week_end"],
                "completed_at_utc": row["completed_at_utc"],
                "source": row["source"],
                "price_component_complete": row.get("price_component_complete") is True,
                "funding_component_complete": row.get("funding_component_complete") is True,
                "cost_component_complete": row.get("cost_component_complete") is True,
                "weekly_return_complete": row.get("complete") is True,
                _ALLOWED_WEEKLY_KEY: row.get(_ALLOWED_WEEKLY_KEY),
                "classification": "RISK_STATE_INITIALIZATION_ONLY",
                "evidence_status": "NOT_PERFORMANCE_EVIDENCE",
            }
            for row in selected
        ],
        "input_complete_count": sum(row.get("complete") is True for row in selected),
        "status": result.status,
        "risk_diagnostic_status": "INITIAL_STATE_DIAGNOSTIC_ONLY" if result.valid else "HALTED_INCOMPLETE_INPUT",
    }
    if result.valid:
        output.update(
            {
                "reference_vol": result.reference_vol,
                "position_scale": result.position_scale,
                "risk_parameters": {
                    "volatility_statistic": "population_std",
                    "week_count": 13,
                    "annualization": "sqrt(52)",
                    "target_annualized_volatility": 0.15,
                    "max_scale": 1.0,
                    "leverage": False,
                },
            }
        )
    else:
        output["halt_reason"] = result.reason
    assert_prestart_state_schema(output)
    return output


def _old_weekly_rows(
    *, parent: Mapping[str, Any], historical_end_equity: float, boundary_delta: float
) -> list[dict[str, Any]]:
    weekly = parent.get("latest_13_week_accounting", {})
    rows = weekly.get("rows") if isinstance(weekly, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 13:
        raise M14IdentityError("M1.3 accepted weekly accounting rows are incomplete")
    after = historical_end_equity - boundary_delta
    output: list[dict[str, Any]] = []
    for raw in reversed(rows):
        try:
            change = float(raw["combined_component"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise M14IdentityError("M1.3 accepted weekly carry rows are malformed") from exc
        prior = after - change
        weekly_return = compute_weekly_return(prior_equity=prior, week_end_equity=after)
        output.append(
            {
                "week_start": str(raw["week_start"]),
                "week_end": str(raw["week_end"]),
                "completed_at_utc": str(raw["completed_at_utc"]),
                "source": "M1_3_CORRECTED_STATE",
                "price_component_complete": raw.get("complete") is True,
                "funding_component_complete": raw.get("complete") is True,
                "cost_component_complete": raw.get("complete") is True,
                "complete": raw.get("complete") is True,
                _ALLOWED_WEEKLY_KEY: weekly_return,
            }
        )
        after = prior
    return list(reversed(output))


def run_incremental_bridge(
    *,
    checkpoint: Mapping[str, Any],
    parent: Mapping[str, Any],
    histories: Sequence[SymbolHistory],
    data_start: date = EXTENSION_START,
    data_end: date = EXTENSION_END,
) -> dict[str, Any]:
    """Continue the parent scheduler over the 18 completed pre-start days."""
    assert_no_historical_full_replay_invoked(data_start=data_start, historical_full_replay_invoked=False)
    validate_extension_window(data_start=data_start, data_end=data_end)
    scheduler = carry_scheduler_state(checkpoint)
    positions = carry_positions_without_reopen(checkpoint)
    history_map = {history.symbol: history for history in histories}
    if not set(positions).issubset(history_map):
        raise M14DataError("checkpoint positions are not present in merged histories")
    historical_accounting = parent.get("accounting", {})
    try:
        historical_end_equity = 1.0 + sum(
            float(historical_accounting[key])
            for key in ("price_component_total", "funding_component_total", "transaction_cost_component_total")
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise M14IdentityError("M1.3 accounting carry state cannot initialize bridge") from exc
    boundary_delta, boundary_complete, boundary_issues = _boundary_delta(
        positions=positions, histories=history_map
    )
    old_weekly = _old_weekly_rows(
        parent=parent,
        historical_end_equity=historical_end_equity,
        boundary_delta=boundary_delta,
    )
    daily_rows: list[dict[str, Any]] = []
    current_equity = historical_end_equity
    transaction_count = 0
    closed_interval_count = 0
    rebalance_count = 0
    unresolved: list[str] = list(boundary_issues)
    funding_issue_count = 0
    attempts: list[dict[str, Any]] = []
    private_deltas: dict[date, float] = {}
    last_successful_signal = date.fromisoformat(str(scheduler["last_successful_signal_day"]))
    last_successful_execution = date.fromisoformat(str(scheduler["last_successful_rebalance_execution_day"]))
    next_due = date.fromisoformat(str(scheduler["next_rebalance_due_day"]))
    pending_retry = bool(scheduler.get("pending_retry", False))
    bar_maps = {history.symbol: history.bars_by_day() for history in histories}
    rules = resolve_rules(variant="control")

    for day in _day_range(data_start, data_end):
        transaction_before = transaction_count
        closed_before = closed_interval_count
        mark_delta, mark_complete, mark_issues, funding_issues = _apply_mark(
            day=day, positions=positions, histories=history_map
        )
        day_issues = list(mark_issues)
        funding_issue_count += funding_issues
        day_delta = mark_delta
        did_rebalance = False
        if day >= next_due:
            signal_day = day - timedelta(days=rules.execution_lag_days)
            signal = None
            if mark_complete:
                universe = _build_universe_fast(histories, bar_maps, signal_day, rules)
                signal = _signal_from_universe(universe, rules)
                if signal is None:
                    day_issues.append(
                        "; ".join(universe.reasons) or f"no Control signal {signal_day.isoformat()}"
                    )
            if signal is None and not day_issues:
                day_issues.append(f"no executable Control signal {signal_day.isoformat()}")
            if signal is not None and not day_issues:
                transition_delta, tx_count, closed_count, transition_issues = _transition_positions(
                    day=day,
                    signal=signal,
                    positions=positions,
                    histories=history_map,
                )
                day_delta += transition_delta
                transaction_count += tx_count
                closed_interval_count += closed_count
                if transition_issues:
                    day_issues.extend(transition_issues)
                else:
                    last_successful_signal = signal_day
                    last_successful_execution = day
                    next_due = day + timedelta(days=rules.rebalance_days)
                    pending_retry = False
                    rebalance_count += 1
                    did_rebalance = True
            if not did_rebalance:
                pending_retry = True
                next_due = day + timedelta(days=1)
            attempts.append(
                {
                    "signal_day": signal_day.isoformat(),
                    "execution_day": day.isoformat(),
                    "status": "SUCCESS" if did_rebalance else "RETRY_PENDING",
                    "state_continuity": "CARRIED_FROM_M1_3_CHECKPOINT",
                }
            )
        if day_issues:
            unresolved.extend(day_issues)
        complete = mark_complete and not day_issues
        if complete:
            current_equity += day_delta
            private_deltas[day] = day_delta
        daily_rows.append(
            {
                "day": day.isoformat(),
                "complete": complete,
                "price_component_complete": mark_complete,
                "funding_component_complete": mark_complete and funding_issues == 0,
                "cost_component_complete": not any("cost" in issue.lower() for issue in day_issues),
                "transaction_count": transaction_count - transaction_before,
                "holding_interval_count": closed_interval_count - closed_before,
                "rebalance_count": 1 if did_rebalance else 0,
                "state_classification": "PRESTART_STATE_INITIALIZATION_ONLY",
            }
        )

    # Current weekly observations are derived from internal equity continuity;
    # the numeric equity baseline is never serialized in M1.4 artifacts.
    current_equity_for_weeks = historical_end_equity - boundary_delta
    weekly_rows = list(old_weekly)
    for week_start in (date(2026, 8, 31), date(2026, 9, 7)):
        week_end = week_start + timedelta(days=6)
        prior = current_equity_for_weeks
        if week_start == date(2026, 8, 31):
            current_equity_for_weeks += boundary_delta
        for day in _day_range(max(EXTENSION_START, week_start), min(EXTENSION_END, week_end)):
            current_equity_for_weeks += private_deltas.get(day, 0.0)
        complete = boundary_complete and all(
            row["complete"] is True
            for row in daily_rows
            if week_start <= date.fromisoformat(row["day"]) <= week_end
        )
        weekly_rows.append(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "completed_at_utc": datetime.combine(week_end + timedelta(days=1), time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
                "source": "M1_4_PRESTART_BRIDGE",
                "price_component_complete": complete,
                "funding_component_complete": complete and funding_issue_count == 0,
                "cost_component_complete": complete,
                "complete": complete,
                _ALLOWED_WEEKLY_KEY: compute_weekly_return(prior_equity=prior, week_end_equity=current_equity_for_weeks) if complete else None,
            }
        )
    selected = select_latest_current_weeks(weekly_rows)
    risk = build_risk_state(selected)
    final_positions = [
        {
            "symbol": symbol,
            "direction": _direction_label(position.direction),
            "direction_code": position.direction,
            "target_notional": position.notional,
            "entry_timestamp_utc": _iso_ms(position.entry_timestamp_ms),
            "last_mark_timestamp_utc": _iso_ms(position.mark_timestamp_ms),
            "last_mark_price": position.mark_price,
        }
        for symbol, position in sorted(positions.items())
    ]
    parent_state = {
        "schema_version": "CURRENT_PARENT_STATE.v1",
        "classification": list(_PRESTART_LABELS),
        "scope": "STATE_METADATA",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "cutoff_utc": PRESTART_CUTOFF.isoformat().replace("+00:00", "Z"),
        "parent_state_complete": not unresolved and bool(final_positions),
        "current_position_count": len(final_positions),
        "current_control_positions": final_positions,
        "last_successful_signal_day": last_successful_signal.isoformat(),
        "last_successful_execution_day": last_successful_execution.isoformat(),
        "next_rebalance_due_day": next_due.isoformat(),
        "pending_retry": pending_retry,
        "unresolved_state_issue_count": len(set(unresolved)),
        "ghost_position_count": 0,
        "no_forward_signal_selected": True,
    }
    bridge_state = {
        "schema_version": "V2_1_M1_4_PRESTART_BRIDGE.v1",
        "classification": list(_PRESTART_LABELS),
        "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "run_id": M14_RUN_ID,
        "cutoff_utc": PRESTART_CUTOFF.isoformat().replace("+00:00", "Z"),
        "extension_start_utc": datetime.combine(EXTENSION_START, time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        "extension_end_utc": PRESTART_CUTOFF.isoformat().replace("+00:00", "Z"),
        "daily_accounting": {
            "complete_day_count": sum(row["complete"] is True for row in daily_rows),
            "observed_day_count": len(daily_rows),
            "component_completeness": {
                "price": all(row["price_component_complete"] for row in daily_rows),
                "funding": all(row["funding_component_complete"] for row in daily_rows),
                "cost": all(row["cost_component_complete"] for row in daily_rows),
            },
            "transaction_count": transaction_count,
            "holding_interval_count": closed_interval_count,
            "rebalance_count": rebalance_count,
        },
        "latest_13_week_endings": [row["week_end"] for row in selected],
        "latest_13_complete": len(selected) == 13 and all(row["complete"] is True for row in selected),
        "funding_issue_count": funding_issue_count,
        "unresolved_state_issue_count": len(set(unresolved)),
        "ghost_position_count": 0,
        "historical_full_replay_invoked": False,
        "forward_artifact_created": False,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "daily_state_metadata": daily_rows,
    }
    assert_prestart_state_schema(parent_state)
    assert_prestart_state_schema(bridge_state)
    return {
        "parent_state": parent_state,
        "bridge_state": bridge_state,
        "risk": risk,
        "weekly_rows": selected,
        "daily_rows": daily_rows,
        "unresolved": sorted(set(unresolved)),
        "funding_issue_count": funding_issue_count,
        "positions": positions,
        "attempts": attempts,
    }


def evaluate_gates(
    *,
    identity_ok: bool,
    checkpoint: Mapping[str, Any],
    manifest: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    bridge: Mapping[str, Any],
    risk: Mapping[str, Any],
    parent_state: Mapping[str, Any],
    m13_before: Mapping[str, str],
    m13_after: Mapping[str, str],
    m13_1_before: Mapping[str, str],
    m13_1_after: Mapping[str, str],
) -> dict[str, str]:
    requests = manifest.get("requests", [])
    request_ok = bool(requests) and all(
        row.get("method") == "GET"
        and row.get("http_status") == 200
        and isinstance(row.get("response_sha256"), str)
        and len(row["response_sha256"]) == 64
        and not row.get("error")
        for row in requests
    )
    missing_daily = manifest.get("coverage", {}).get("missing_daily_by_symbol", {})
    ambiguous_candidates = manifest.get("candidate_discovery", {}).get("ambiguous_candidate_count", 1)
    selected = tuple(bridge.get("latest_13_week_endings", ()))
    daily = bridge.get("daily_accounting", {})
    component = daily.get("component_completeness", {})
    weekly_complete = risk.get("input_complete_count") == 13 and risk.get("selected_count") == 13
    identity = checkpoint.get("m1_3_overlay_sha256") == M13_OVERLAY_SHA256
    gates = {
        "B0_identity": "PASS" if identity_ok and identity else "FAIL",
        "B1_checkpoint_continuity": "PASS"
        if checkpoint.get("historical_full_replay_invoked") is False
        and checkpoint.get("accounting_state_continuity_hash")
        and checkpoint.get("m1_3_result_commit") == M13_RESULT_COMMIT
        else "FAIL",
        "B2_prestart_data_provenance": "PASS"
        if request_ok and manifest.get("fetch_summary", {}).get("official_get_only") is True
        and not missing_daily
        else "FAIL",
        "B3_pit_universe_extension": "PASS"
        if ambiguous_candidates == 0
        and manifest.get("candidate_discovery", {}).get("pit_membership_requires_timestamped_onboard_and_completed_bar") is True
        else "FAIL",
        "B4_lifecycle_extension": "PASS"
        if lifecycle.get("ambiguous_candidates") == []
        and lifecycle.get("no_fabricated_lifecycle") is True
        else "FAIL",
        "B5_scheduler_continuity": "PASS"
        if checkpoint.get("scheduler_clock_state", {}).get("scheduler_initialized_from_checkpoint") is True
        and checkpoint.get("scheduler_clock_state", {}).get("rebalance_interval_days") == 7
        else "FAIL",
        "B6_position_continuity": "PASS"
        if parent_state.get("current_position_count", 0) > 0
        and parent_state.get("ghost_position_count") == 0
        else "FAIL",
        "B7_incremental_accounting_integrity": "PASS"
        if daily.get("observed_day_count") == (EXTENSION_END - EXTENSION_START).days + 1
        and daily.get("complete_day_count") == daily.get("observed_day_count")
        and all(component.get(name) is True for name in ("price", "funding", "cost"))
        else "FAIL",
        "B8_funding_coverage": "PASS" if bridge.get("funding_issue_count") == 0 else "FAIL",
        "B9_current_latest_13_selection": "PASS"
        if len(selected) == 13
        and len(set(selected)) == 13
        and "2026-09-06" in selected
        and "2026-09-13" in selected
        and "2026-06-07" not in selected
        else "FAIL",
        "B10_current_latest_13_completeness": "PASS" if weekly_complete else "FAIL",
        "B11_risk_input": "PASS"
        if risk.get("status") == "VALID" and risk.get("risk_diagnostic_status") == "INITIAL_STATE_DIAGNOSTIC_ONLY"
        else "FAIL",
        "B12_current_parent_state": "PASS"
        if parent_state.get("parent_state_complete") is True
        and parent_state.get("unresolved_state_issue_count") == 0
        and parent_state.get("ghost_position_count") == 0
        else "FAIL",
        "B13_anchor_still_frozen": "PASS"
        if V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
        and FORWARD_ANCHOR_SHA256 == verify_v2_1_forward_anchor_hash()
        else "FAIL",
        "B14_no_forward_evidence": "PASS"
        if bridge.get("forward_artifact_created") is False
        and parent_state.get("no_forward_signal_selected") is True
        else "FAIL",
        "B15_no_historical_full_replay": "PASS"
        if bridge.get("historical_full_replay_invoked") is False
        and checkpoint.get("historical_full_replay_invoked") is False
        else "FAIL",
    }
    if m13_before != m13_after or m13_1_before != m13_1_after:
        gates["B0_identity"] = "FAIL"
    return gates


def _render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        str(report["decision"]),
        "",
        "# XS-LOWVOL V2.1-M1.4 Current Prestart Bridge",
        "",
        "This artifact is `PRESTART_STATE_INITIALIZATION_ONLY`, `NOT_FORWARD`, and `NOT_VALIDATION_EVIDENCE`.",
        "No first Forward signal was selected and no runtime evidence was published.",
        "Historical accounting value remains opaque carry state; no numeric historical baseline or aggregate result is emitted here.",
        "",
        "## Frozen identity",
        "",
        f"- Code commit used by run: `{report['code_commit_used_by_run']}`",
        f"- Cutoff UTC: `{report['cutoff_utc']}`",
        f"- M1.3 checkpoint SHA-256: `{report['m1_3_checkpoint_sha256']}`",
        f"- M1.3 lifecycle overlay SHA-256: `{report['m1_3_overlay_sha256']}`",
        f"- Pre-start extension manifest SHA-256: `{report['prestart_extension_manifest_sha256']}`",
        f"- Pre-start lifecycle extension SHA-256: `{report['prestart_lifecycle_extension_sha256']}`",
        "",
        "## B0-B15",
        "",
    ]
    lines.extend(f"- `{name}`: `{value}`" for name, value in report["gates"].items())
    lines.extend(
        [
            "",
            "## State metadata",
            "",
            f"- Latest-13 week endings: `{', '.join(report['latest_13_week_endings'])}`",
            f"- Latest-13 complete: `{report['latest_13_complete']}`",
            f"- Last successful signal day: `{report['last_successful_signal_day']}`",
            f"- Last successful execution day: `{report['last_successful_execution_day']}`",
            f"- Next rebalance due day: `{report['next_rebalance_due_day']}` (not Forward Day 1)",
            f"- Pending retry: `{report['pending_retry']}`",
            f"- Current position count: `{report['current_position_count']}`",
            f"- Ghost position count: `{report['ghost_position_count']}`",
            f"- Funding issue count: `{report['funding_issue_count']}`",
            "",
            "## Safety",
            "",
            "- LIVE_TRADING: `False`",
            "- PAPER_ONLY: `True`",
            "- HTTP: `GET-only`",
            "- No optimization, no V2, no M2, no Forward evidence.",
        ]
    )
    if report.get("risk_status") == "VALID":
        lines.extend(
            [
                "",
                "## Risk initialization",
                "",
                f"- Reference volatility: `{report['reference_vol']}`",
                f"- Position scale: `{report['position_scale']}`",
                "- Status: `INITIAL_STATE_DIAGNOSTIC_ONLY`",
            ]
        )
    return "\n".join(lines) + "\n"


def run_formal_bridge(*, approval: str) -> dict[str, Any]:
    if approval != M14_APPROVAL:
        raise M14Error(f"M1.4 requires explicit approval {M14_APPROVAL!r}")
    if M14_OUTPUT_DIR.exists():
        raise M14Error("M1.4 output directory already exists; corrected reruns are forbidden")
    code_commit = current_commit()
    if not _is_ancestor(M14_BASE_COMMIT, code_commit):
        raise M14IdentityError("current code is not based on the approved M1.4 base commit")
    if not _git_status_clean():
        raise M14Error("M1.4 formal run requires a clean worktree")
    assert_no_historical_full_replay_invoked(data_start=EXTENSION_START, historical_full_replay_invoked=False)
    identity = verify_frozen_identity()
    m13_before = snapshot_m13_artifacts()
    m13_1_before = snapshot_m13_1_scope()
    if m13_before != M13_ARTIFACT_SHA256 or m13_1_before != M13_1_SCOPE_SHA256:
        raise M14IdentityError("accepted M1.3 or M1.3.1 scope artifacts changed before run")
    evidence_path = PROJECT_ROOT / "research" / "evidence" / "XS-LOWVOL-V1.json"
    evidence = _load_json(evidence_path)
    if evidence.get("status") != "EVIDENCE_STALE":
        raise M14IdentityError("runtime Evidence is not EVIDENCE_STALE")

    dataset = load_verified_dataset()
    parent = _load_json(M13_DIR / "V2_1_M1_3_PARENT_RECONSTRUCTION.json")
    checkpoint, _, base_histories = build_m13_checkpoint(dataset=dataset, write=False)
    merged_histories, manifest, lifecycle, _ = fetch_prestart_extension(histories=base_histories)
    bridge = run_incremental_bridge(
        checkpoint=checkpoint,
        parent=parent,
        histories=merged_histories,
        data_start=EXTENSION_START,
        data_end=EXTENSION_END,
    )
    m13_after = snapshot_m13_artifacts()
    m13_1_after = snapshot_m13_1_scope()
    gates = evaluate_gates(
        identity_ok=True,
        checkpoint=checkpoint,
        manifest=manifest,
        lifecycle=lifecycle,
        bridge=bridge["bridge_state"],
        risk=bridge["risk"],
        parent_state=bridge["parent_state"],
        m13_before=m13_before,
        m13_after=m13_after,
        m13_1_before=m13_1_before,
        m13_1_after=m13_1_after,
    )
    decision = "V2.1-M1.4 PRESTART READY" if all(value == "PASS" for value in gates.values()) else "V2.1-M1.4 PRESTART NOT_READY"

    checkpoint_sha = _write_json(M14_CHECKPOINT_PATH, checkpoint)
    manifest_sha = _write_json(M14_EXTENSION_MANIFEST_PATH, manifest)
    lifecycle_sha = _write_json(M14_LIFECYCLE_PATH, lifecycle)
    risk_sha = _write_json(M14_RISK_PATH, bridge["risk"])
    parent_sha = _write_json(M14_PARENT_STATE_PATH, bridge["parent_state"])
    bridge_payload = dict(bridge["bridge_state"])
    bridge_payload.update(
        {
            "decision": decision,
            "identity": identity,
            "base_commit": M14_BASE_COMMIT,
            "code_commit_used_by_run": code_commit,
            "m1_3_result_commit": M13_RESULT_COMMIT,
            "m1_3_code_commit": M13_CODE_COMMIT,
            "m1_3_checkpoint_sha256": checkpoint_sha,
            "m1_3_overlay_sha256": M13_OVERLAY_SHA256,
            "prestart_extension_manifest_sha256": manifest_sha,
            "prestart_lifecycle_extension_sha256": lifecycle_sha,
            "current_latest_13_risk_input_sha256": risk_sha,
            "current_parent_state_sha256": parent_sha,
            "gates": gates,
            "failed_gates": [name for name, value in gates.items() if value != "PASS"],
            "extension_request_count": len(manifest["requests"]),
            "symbols_covered": manifest["coverage"]["symbols_fetched"],
            "new_listings_discovered": [row["symbol"] for row in lifecycle["records"]],
            "lifecycle_events_discovered": len(lifecycle["records"]),
            "last_successful_signal_day": bridge["parent_state"]["last_successful_signal_day"],
            "last_successful_execution_day": bridge["parent_state"]["last_successful_execution_day"],
            "next_rebalance_due_day": bridge["parent_state"]["next_rebalance_due_day"],
            "pending_retry": bridge["parent_state"]["pending_retry"],
            "current_position_count": bridge["parent_state"]["current_position_count"],
            "ghost_position_count": bridge["parent_state"]["ghost_position_count"],
            "funding_issue_count": bridge["funding_issue_count"],
            "latest_13_week_endings": bridge["risk"]["latest_week_endings"],
            "latest_13_complete": bridge["bridge_state"]["latest_13_complete"],
            "m1_3_artifacts_unchanged": m13_before == m13_after,
            "m1_3_1_scope_unchanged": m13_1_before == m13_1_after,
            "evidence_status": evidence["status"],
            "anchor_status": V21_FORWARD_ANCHOR_STATUS,
            "forward_started": False,
            "first_forward_signal_selected": False,
            "parameter_optimization": False,
            "m2_started": False,
            "live_trading": False,
            "paper_only": True,
            "http_method_policy": "GET_ONLY",
        }
    )
    bridge_sha = _write_json(M14_REPORT_PATH, bridge_payload)
    markdown = _render_markdown(
        {
            **bridge_payload,
            "risk_status": bridge["risk"]["status"],
            "reference_vol": bridge["risk"].get("reference_vol"),
            "position_scale": bridge["risk"].get("position_scale"),
        }
    )
    M14_MARKDOWN_PATH.write_text(markdown, encoding="utf-8")
    return {
        **bridge_payload,
        "checkpoint_sha256": checkpoint_sha,
        "manifest_sha256": manifest_sha,
        "lifecycle_sha256": lifecycle_sha,
        "risk_sha256": risk_sha,
        "parent_sha256": parent_sha,
        "bridge_sha256": bridge_sha,
        "markdown_path": M14_MARKDOWN_PATH.as_posix(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    result = run_formal_bridge(approval=args.approval)
    print(result["decision"])
    print(json.dumps(_canonical(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXTENSION_END",
    "EXTENSION_START",
    "M14_APPROVAL",
    "M14_BASE_COMMIT",
    "M14_OUTPUT_DIR",
    "M14_REPORT_PATH",
    "PRESTART_CUTOFF",
    "assert_no_historical_full_replay_invoked",
    "assert_prestart_state_schema",
    "build_m13_checkpoint",
    "build_risk_state",
    "candidate_has_pit_proof",
    "carry_positions_without_reopen",
    "carry_scheduler_state",
    "compute_weekly_return",
    "evaluate_gates",
    "run_formal_bridge",
    "run_incremental_bridge",
    "select_latest_current_weeks",
    "validate_extension_window",
    "verify_frozen_identity",
]
