"""M1-A.1 corrective-pass tests using only temporary and mocked data."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from backtest.m1_protocol import (
    M1_APPROVED_PROTOCOL_SHA256,
    ProtocolError,
    load_protocol,
    protocol_sha256,
    read_protocol_hash,
    validate_protocol,
    verify_protocol_hash,
)
from backtest.xs_data_quality import (
    HoldingInterval,
    funding_pnl_for_hold,
    signed_funding_pnl,
    validate_funding_coverage_for_holds,
    validate_funding_structure,
)
from backtest.xs_history import (
    DailyBar,
    DiscoveredArchiveRecord,
    FundingEvent,
    HistoryDataError,
    acquire_official_archive_listing,
    archive_url,
    build_dataset_manifest,
    build_discovery_catalog,
    download_archive_record,
    file_sha256,
    make_symbol_history,
    parse_archive_ref,
)


UTC = timezone.utc


def _ms(day: date, hour: int = 0) -> int:
    return int((datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(hours=hour)).timestamp() * 1000)


def _history(symbol: str, events: tuple[FundingEvent, ...] = ()):
    day = date(2021, 1, 1)
    bar = DailyBar(
        symbol=symbol,
        day=day,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        quote_volume=60_000_000.0,
        open_time_ms=_ms(day),
        close_time_ms=_ms(day, 23) + 3_599_999,
    )
    return make_symbol_history(
        symbol,
        [bar],
        events,
        currently_active=True,
    )


def _catalog_listing(symbols: tuple[str, ...], months: tuple[str, ...] = ("2021-01",)) -> list[str]:
    return [
        archive_url(symbol, kind, month)
        for symbol in symbols
        for month in months
        for kind in ("daily_klines", "funding")
    ]


def test_approved_protocol_pin_is_three_way_verified():
    assert verify_protocol_hash() == M1_APPROVED_PROTOCOL_SHA256
    assert read_protocol_hash() == M1_APPROVED_PROTOCOL_SHA256


@pytest.mark.parametrize("recompute_sidecar", [False, True])
def test_protocol_yaml_tampering_fails_even_when_sidecar_is_recomputed(tmp_path: Path, recompute_sidecar: bool):
    altered = load_protocol()
    altered["bootstrap"]["seed"] = 20260916
    yaml_path = tmp_path / "protocol.yaml"
    hash_path = tmp_path / "protocol.sha256"
    yaml_path.write_text(yaml.safe_dump(altered, sort_keys=False), encoding="utf-8")
    hash_path.write_text(
        (protocol_sha256(altered) if recompute_sidecar else M1_APPROVED_PROTOCOL_SHA256) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ProtocolError, match="approved|mismatch"):
        verify_protocol_hash(yaml_path, hash_path)


def test_protocol_threshold_and_strategy_identity_tampering_is_rejected():
    altered_threshold = load_protocol()
    altered_threshold["hard_gates"]["G3_weekly_sharpe"]["weekly_sharpe_gt"] = 0.8
    with pytest.raises(ProtocolError, match="threshold"):
        validate_protocol(altered_threshold, require_current_specs=False)

    altered_strategy = load_protocol()
    altered_strategy["shadow"]["spec_hash"] = "0" * 64
    with pytest.raises(ProtocolError, match="Shadow spec hash"):
        validate_protocol(altered_strategy, require_current_specs=False)

    altered_window = load_protocol()
    altered_window["data_end"] = date(2026, 9, 1)
    with pytest.raises(ProtocolError, match="data_start/data_end"):
        validate_protocol(altered_window, require_current_specs=False)


def test_discovery_catalog_manifest_retains_dead_and_broken_symbols():
    listing = _catalog_listing(("AUSDT", "BUSDT", "DEADUSDT", "BROKENUSDT"))
    catalog = build_discovery_catalog(
        listing,
        source="mock://official-index",
        prefix="data/futures/um/monthly",
        retrieved_at="2026-09-15T00:00:00+00:00",
        content="\n".join(listing),
    )
    histories = [
        _history("AUSDT"),
        _history("BUSDT"),
        _history("DEADUSDT"),
    ]
    dead = histories[-1]
    dead = dead.__class__(
        symbol=dead.symbol,
        daily_bars=dead.daily_bars,
        funding_events=dead.funding_events,
        lifecycle=dead.lifecycle.__class__(
            **{
                **dead.lifecycle.__dict__,
                "currently_active": False,
                "delisted_at": date(2021, 1, 2),
            }
        ),
        quote_asset=dead.quote_asset,
        contract_type=dead.contract_type,
    )
    histories[-1] = dead
    manifest = build_dataset_manifest(histories, catalog=catalog)
    assert manifest["number_of_symbols_discovered"] == 4
    assert {item["symbol"] for item in manifest["symbols"]} == {
        "AUSDT",
        "BUSDT",
        "DEADUSDT",
        "BROKENUSDT",
    }
    broken = next(item for item in manifest["symbols"] if item["symbol"] == "BROKENUSDT")
    assert broken["dataset_status"] != "usable"
    assert broken["exclusion_reason"]
    assert next(item for item in manifest["symbols"] if item["symbol"] == "DEADUSDT")["currently_active"] is False


def test_manifest_records_each_archive_and_checksum_separately(tmp_path: Path):
    listing = _catalog_listing(("AUSDT",), ("2021-01", "2021-02", "2021-03"))
    catalog = build_discovery_catalog(listing, retrieved_at="2026-09-15T00:00:00+00:00")
    records: list[DiscoveredArchiveRecord] = []
    for index, archive in enumerate(catalog.archives):
        local = tmp_path / f"archive-{index}.zip"
        local.write_bytes(f"archive-{index}".encode("ascii"))
        records.append(
            DiscoveredArchiveRecord(
                symbol=archive.symbol,
                kind=archive.kind,
                month=archive.month,
                url=archive.url,
                local_path=str(local),
                local_sha256=file_sha256(local),
                official_checksum=file_sha256(local),
                checksum_verified=True,
                download_status="downloaded",
                normalize_status="normalized",
                quality_status="passed",
            )
        )
    manifest = build_dataset_manifest(
        [_history("AUSDT")],
        catalog=catalog,
        archive_records=records,
    )
    assert manifest["raw_file_count"] == 6
    assert len(manifest["raw_files"]) == 6
    symbol = manifest["symbols"][0]
    assert len(symbol["source_files"]) == 6
    assert len(symbol["source_checksums"]) == 6
    assert all(item["source_url"].startswith("https://data.binance.vision/") for item in symbol["source_files"])


class _Response:
    def __init__(self, status_code: int, content: bytes = b"", text: str | None = None):
        self.status_code = status_code
        self.content = content
        self.text = text if text is not None else content.decode("utf-8", "replace")


class _ListingSession:
    def __init__(self, response: _Response):
        self.response = response
        self.calls: list[tuple[str, int]] = []

    def get(self, url: str, *, timeout: int):
        self.calls.append((url, timeout))
        return self.response


def test_official_listing_interface_is_explicit_and_get_only():
    key = _s3_key("AUSDT", "daily_klines", "2021-01")
    payload = (
        "<ListBucketResult>"
        "<Prefix>data/futures/um/monthly/</Prefix>"
        "<IsTruncated>false</IsTruncated>"
        f"<Contents><Key>{key}</Key></Contents>"
        "</ListBucketResult>"
    )
    session = _ListingSession(_Response(200, payload.encode("utf-8"), payload))
    catalog = acquire_official_archive_listing(
        prefix="data/futures/um/monthly",
        session=session,
        retrieved_at="2026-09-15T00:00:00+00:00",
    )
    assert catalog.number_of_symbols_discovered == 1
    assert catalog.source.startswith("https://s3-ap-northeast-1.amazonaws.com/")
    assert catalog.content_sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert len(session.calls) == 1
    assert session.calls[0][1] == 60


def _s3_key(symbol: str, kind: str, month: str) -> str:
    return archive_url(symbol, kind, month).split("https://data.binance.vision/", 1)[1]


def _s3_page(
    keys: list[str],
    *,
    is_truncated: bool,
    marker: str | None = None,
    next_marker: str | None = None,
    prefix: str = "data/futures/um/monthly/",
) -> bytes:
    marker_xml = f"<Marker>{marker}</Marker>" if marker is not None else ""
    next_xml = f"<NextMarker>{next_marker}</NextMarker>" if next_marker is not None else ""
    contents = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in keys)
    return (
        "<ListBucketResult>"
        f"<Prefix>{prefix}</Prefix>"
        f"{marker_xml}"
        f"<IsTruncated>{str(is_truncated).lower()}</IsTruncated>"
        f"{next_xml}{contents}</ListBucketResult>"
    ).encode("utf-8")


class _PagedListingSession:
    def __init__(self, responses: list[_Response]):
        self.responses = responses
        self.calls: list[tuple[str, int]] = []

    def get(self, url: str, *, timeout: int):
        self.calls.append((url, timeout))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return response


def test_official_listing_paginates_all_pages_before_marking_complete():
    keys = [
        _s3_key(f"S{index:04d}USDT", "daily_klines", "2021-01")
        for index in range(2123)
    ]
    pages = [
        _Response(200, _s3_page(keys[:1000], is_truncated=True, next_marker="A")),
        _Response(200, _s3_page(keys[1000:2000], is_truncated=True, marker="A", next_marker="B")),
        _Response(200, _s3_page(keys[2000:], is_truncated=False, marker="B")),
    ]
    session = _PagedListingSession(pages)
    catalog = acquire_official_archive_listing(
        session=session,
        retrieved_at="2026-09-15T00:00:00+00:00",
    )
    assert len(session.calls) == 3
    assert catalog.listing_page_count == 3
    assert catalog.listing_complete is True
    assert [page.key_count for page in catalog.pages] == [1000, 1000, 123]
    assert len(catalog.archives) == 2123
    assert catalog.number_of_symbols_discovered == 2123
    assert "marker=A" in session.calls[1][0]
    assert "marker=B" in session.calls[2][0]
    assert catalog.content_sha256 == hashlib.sha256(
        b"".join(response.content for response in pages)
    ).hexdigest()
    assert all(len(page.page_content_sha256) == 64 for page in catalog.pages)
    assert catalog.pages[0].first_key == keys[0]
    assert catalog.pages[-1].last_key == keys[-1]


@pytest.mark.parametrize(
    "responses,match",
    [
        ([_Response(200, _s3_page([_s3_key("AUSDT", "daily_klines", "2021-01")], is_truncated=True))], "marker"),
        (
            [
                _Response(200, _s3_page([_s3_key("AUSDT", "daily_klines", "2021-01")], is_truncated=True, next_marker="A")),
                _Response(200, _s3_page([_s3_key("BUSDT", "daily_klines", "2021-01")], is_truncated=True, marker="A", next_marker="A")),
            ],
            "marker",
        ),
        (
            [
                _Response(200, _s3_page([_s3_key("AUSDT", "daily_klines", "2021-01")], is_truncated=True, next_marker="A")),
                _Response(500, b"server error"),
            ],
            "HTTP 500",
        ),
        ([_Response(200, b"<ListBucketResult>")], "malformed"),
        (
            [
                _Response(200, _s3_page([_s3_key("BUSDT", "daily_klines", "2021-01")], is_truncated=True, next_marker="A")),
                _Response(200, _s3_page([_s3_key("AUSDT", "daily_klines", "2021-01")], is_truncated=False, marker="A")),
            ],
            "ordering",
        ),
        (
            [_Response(200, _s3_page([_s3_key("AUSDT", "daily_klines", "2021-01")], is_truncated=False, prefix="wrong/"))],
            "prefix mismatch",
        ),
    ],
)
def test_official_listing_pagination_fails_closed(responses: list[_Response], match: str):
    with pytest.raises(HistoryDataError, match=match):
        acquire_official_archive_listing(session=_PagedListingSession(responses))


def test_official_listing_fails_closed_when_max_pages_is_exceeded():
    key = _s3_key("AUSDT", "daily_klines", "2021-01")
    session = _PagedListingSession(
        [
            _Response(200, _s3_page([key], is_truncated=True, next_marker="A")),
            _Response(200, _s3_page([key], is_truncated=True, marker="A", next_marker="B")),
        ]
    )
    with pytest.raises(HistoryDataError, match="max_pages"):
        acquire_official_archive_listing(session=session, max_pages=2)


def test_official_listing_rejects_conflicting_duplicate_key_metadata():
    key = _s3_key("AUSDT", "daily_klines", "2021-01")
    payload = (
        "<ListBucketResult>"
        "<Prefix>data/futures/um/monthly/</Prefix>"
        "<IsTruncated>false</IsTruncated>"
        f"<Contents><Key>{key}</Key><Size>1</Size></Contents>"
        f"<Contents><Key>{key}</Key><Size>2</Size></Contents>"
        "</ListBucketResult>"
    ).encode("utf-8")
    with pytest.raises(HistoryDataError, match="conflicting duplicate"):
        acquire_official_archive_listing(
            session=_PagedListingSession([_Response(200, payload)])
        )


def test_official_listing_rejects_incomplete_catalog_at_dataset_boundary():
    key = _s3_key("AUSDT", "daily_klines", "2021-01")
    partial = build_discovery_catalog(
        [key],
        pages=(),
        listing_complete=False,
    )
    with pytest.raises(HistoryDataError, match="DATA_INVALID"):
        build_dataset_manifest([], catalog=partial)
    with pytest.raises(HistoryDataError, match="DATA_INVALID"):
        acquire_official_archive_listing(
            session=_PagedListingSession([_Response(200, _s3_page([key], is_truncated=True))]),
            max_pages=1,
        )


def test_official_listing_allows_identical_duplicate_key_deterministically():
    first = _s3_key("AUSDT", "daily_klines", "2021-01")
    second = _s3_key("BUSDT", "daily_klines", "2021-01")
    session = _PagedListingSession(
        [
            _Response(200, _s3_page([first], is_truncated=True, next_marker="A")),
            _Response(200, _s3_page([first, second], is_truncated=False, marker="A")),
        ]
    )
    catalog = acquire_official_archive_listing(session=session)
    assert len(session.calls) == 2
    assert [archive.symbol for archive in catalog.archives] == ["AUSDT", "BUSDT"]


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AUSDT-1d-2021-01.csv", "open_time,open\n")
    return buffer.getvalue()


class _ArchiveSession:
    def __init__(self, archive_bytes: bytes, checksum: str):
        self.archive_bytes = archive_bytes
        self.checksum = checksum
        self.calls: list[str] = []

    def get(self, url: str, *, timeout: int):
        self.calls.append(url)
        if url.endswith(".CHECKSUM"):
            return _Response(200, text=f"{self.checksum}  AUSDT.zip")
        return _Response(200, self.archive_bytes)


def test_archive_download_verifies_official_checksum_and_fails_closed(tmp_path: Path):
    ref = parse_archive_ref(archive_url("AUSDT", "daily_klines", "2021-01"))
    assert ref is not None
    archive_bytes = _zip_bytes()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    session = _ArchiveSession(archive_bytes, digest)
    record = download_archive_record(ref, raw_dir=tmp_path / "good", session=session)
    assert record.download_status == "downloaded"
    assert record.local_sha256 == digest
    assert record.official_checksum == digest
    assert record.checksum_verified is True
    assert session.calls == [ref.url, f"{ref.url}.CHECKSUM"]

    mismatch = _ArchiveSession(archive_bytes, "0" * 64)
    with pytest.raises(HistoryDataError, match="DATA_INVALID"):
        download_archive_record(ref, raw_dir=tmp_path / "bad", session=mismatch)


def test_funding_structure_dedupes_identical_but_rejects_conflicts_and_no_hold_is_not_a_zero_pnl():
    first = FundingEvent("AUSDT", _ms(date(2021, 1, 1), 4), 0.001, 4.0)
    second = FundingEvent("AUSDT", _ms(date(2021, 1, 1), 8), -0.001, 4.0)
    identical = _history("AUSDT", (first, first, second))
    assert validate_funding_structure(identical) == ()
    no_hold = validate_funding_coverage_for_holds([identical], [])
    assert no_hold.structural_passed and no_hold.holds_passed
    with pytest.raises(ValueError, match="不能伪造"):
        funding_pnl_for_hold(100.0, 1, [])

    conflicting = _history("AUSDT", (first, FundingEvent("AUSDT", first.funding_time_ms, 0.002, 4.0), second))
    assert not validate_funding_coverage_for_holds([conflicting], []).structural_passed


@pytest.mark.parametrize("interval_hours", [4.0, 8.0])
def test_funding_hold_coverage_is_separate_and_requires_every_real_settlement(interval_hours: float):
    day = date(2021, 1, 1)
    step = int(interval_hours)
    events = tuple(
        FundingEvent("AUSDT", _ms(day, hour), 0.001, interval_hours)
        for hour in range(step, step * 4, step)
    )
    history = _history("AUSDT", events)
    holding = HoldingInterval("AUSDT", _ms(day, 1), _ms(day, step * 3))
    complete = validate_funding_coverage_for_holds(
        [history], [holding], expected_interval_hours=interval_hours
    )
    assert complete.structural_passed and complete.holds_passed
    incomplete = _history("AUSDT", tuple(event for event in events if event.funding_time_ms != _ms(day, step * 2)))
    failed = validate_funding_coverage_for_holds(
        [incomplete], [holding], expected_interval_hours=interval_hours
    )
    assert not failed.holds_passed


def test_funding_sign_is_long_pays_short_receives_for_real_events():
    event = FundingEvent("AUSDT", _ms(date(2021, 1, 1), 8), 0.001, 8.0)
    assert funding_pnl_for_hold(1_000.0, 1, [event]) == pytest.approx(-1.0)
    assert signed_funding_pnl(1_000.0, -1, [event]) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "hours,intervals",
    [
        ((8, 16, 20, 24), (8.0, 8.0, 4.0, 4.0)),
        ((4, 8, 16, 24), (4.0, 4.0, 8.0, 8.0)),
    ],
)
def test_funding_hold_coverage_uses_each_event_interval_across_transitions(
    hours: tuple[int, ...], intervals: tuple[float, ...]
):
    day = date(2021, 1, 1)
    events = tuple(
        FundingEvent("AUSDT", _ms(day, hour), 0.001, interval)
        for hour, interval in zip(hours, intervals)
    )
    report = validate_funding_coverage_for_holds(
        [_history("AUSDT", events)],
        [HoldingInterval("AUSDT", _ms(day, 1), _ms(day, hours[-1]))],
        expected_interval_hours=4.0,
    )
    assert report.structural_passed
    assert report.holds_passed


@pytest.mark.parametrize(
    "hours,intervals",
    [
        ((8, 16, 28, 32), (8.0, 8.0, 4.0, 4.0)),
        ((4, 8, 20, 28), (4.0, 4.0, 8.0, 8.0)),
    ],
)
def test_funding_hold_coverage_fails_closed_on_missing_variable_transition(
    hours: tuple[int, ...], intervals: tuple[float, ...]
):
    day = date(2021, 1, 1)
    events = tuple(
        FundingEvent("AUSDT", _ms(day, hour), 0.001, interval)
        for hour, interval in zip(hours, intervals)
    )
    report = validate_funding_coverage_for_holds(
        [_history("AUSDT", events)],
        [HoldingInterval("AUSDT", _ms(day, 1), _ms(day, hours[-1]))],
    )
    assert not report.structural_passed
    assert not report.holds_passed
    assert "FUNDING_COVERAGE_GAP" in {issue.code for issue in report.issues}


def test_nonheld_funding_gap_is_structural_but_does_not_contaminate_held_sequence():
    day = date(2021, 1, 1)
    held_symbol = _history(
        "AUSDT",
        tuple(
            FundingEvent("AUSDT", _ms(day, hour), 0.001, 4.0)
            for hour in (4, 8, 12, 16)
        ),
    )
    same_symbol_gap_after_exit = _history(
        "BUSDT",
        tuple(
            FundingEvent("BUSDT", _ms(day, hour), 0.001, 4.0)
            for hour in (4, 8, 12, 16, 28)
        ),
    )
    unrelated_gap = _history(
        "CUSDT",
        (
            FundingEvent("CUSDT", _ms(day, 4), 0.001, 4.0),
            FundingEvent("CUSDT", _ms(day, 8), 0.001, 4.0),
            FundingEvent("CUSDT", _ms(day, 20), 0.001, 4.0),
        ),
    )
    report = validate_funding_coverage_for_holds(
        [held_symbol, same_symbol_gap_after_exit, unrelated_gap],
        [
            HoldingInterval("AUSDT", _ms(day, 1), _ms(day, 8)),
            HoldingInterval("BUSDT", _ms(day, 1), _ms(day, 8)),
        ],
    )
    assert not report.structural_passed
    assert report.holds_passed
