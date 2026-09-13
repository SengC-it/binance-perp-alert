"""状态层测试：连续计数、冷却时间、告警队列、权益统计、资金费累计。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models import Alert, Severity
from src.store import Store

NOW = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def make_alert(rule_id="margin_ratio_low", key="BTCUSDT:LONG", severity=Severity.WARN) -> Alert:
    return Alert(rule_id, key, severity, "标题", "正文", symbol="BTCUSDT", created_at=NOW)


def test_bump_streak_counts_up_and_resets(store):
    assert store.bump_streak("r", "k", True, NOW) == 1
    assert store.bump_streak("r", "k", True, NOW) == 2
    assert store.bump_streak("r", "k", False, NOW) == 0
    assert store.bump_streak("r", "k", True, NOW) == 1


def test_streak_is_isolated_per_dedup_key(store):
    store.bump_streak("r", "A", True, NOW)
    store.bump_streak("r", "A", True, NOW)
    assert store.bump_streak("r", "B", True, NOW) == 1


def test_mark_fired_resets_streak_and_records_time(store):
    store.bump_streak("r", "k", True, NOW)
    store.bump_streak("r", "k", True, NOW)
    store.mark_fired("r", "k", NOW)
    assert store.last_fired_at("r", "k") == NOW
    assert store.bump_streak("r", "k", True, NOW) == 1


def test_last_fired_at_none_for_unknown_rule(store):
    assert store.last_fired_at("nope", "nope") is None


def test_alert_queue_pending_then_sent(store):
    alert_id = store.insert_alert(make_alert())
    pending = store.pending_alerts()
    assert [a.alert_id for a in pending] == [alert_id]
    store.mark_sent(alert_id, NOW)
    assert store.pending_alerts() == []
    assert store.sent_count_since(NOW - timedelta(hours=1)) == 1


def test_sent_count_window(store):
    old = NOW - timedelta(hours=5)
    store.insert_alert(make_alert(), sent_at=old)
    store.insert_alert(make_alert(), sent_at=NOW)
    assert store.sent_count_since(NOW - timedelta(hours=1)) == 1
    assert store.sent_count_since(NOW - timedelta(hours=6)) == 2


def test_pending_excludes_sent_and_keeps_order(store):
    first = store.insert_alert(make_alert("r1"))
    second = store.insert_alert(make_alert("r2"))
    store.mark_sent(first, NOW)
    assert [a.alert_id for a in store.pending_alerts()] == [second]


def test_heartbeat_roundtrip(store):
    assert store.heartbeat_last_ok("positions") is None
    store.heartbeat_ok("positions", NOW, "ok")
    assert store.heartbeat_last_ok("positions") == NOW
    store.heartbeat_ok("positions", NOW + timedelta(seconds=60), "again")
    assert store.heartbeat_last_ok("positions") == NOW + timedelta(seconds=60)


def test_equity_first_since_and_peak(store):
    day_start = NOW - timedelta(hours=2)
    store.record_equity(NOW - timedelta(hours=1), 10000.0)
    store.record_equity(NOW, 9500.0)
    assert store.first_equity_since(day_start) == 10000.0
    assert store.first_equity_since(NOW - timedelta(minutes=30)) == 9500.0
    assert store.peak_equity() == 10000.0


def test_equity_snapshot_is_idempotent_per_timestamp(store):
    store.record_equity(NOW, 10000.0)
    store.record_equity(NOW, 12000.0)
    assert store.peak_equity() == 12000.0


def test_position_first_seen_is_stable(store):
    first = store.position_first_seen("BTCUSDT", "LONG", NOW)
    again = store.position_first_seen("BTCUSDT", "LONG", NOW + timedelta(minutes=10))
    assert first == again == NOW


def test_funding_cost_accumulates(store):
    store.position_first_seen("BTCUSDT", "LONG", NOW)
    assert store.add_funding_cost("BTCUSDT", "LONG", 0.5) == pytest.approx(0.5)
    assert store.add_funding_cost("BTCUSDT", "LONG", 0.25) == pytest.approx(0.75)


def test_funding_cost_can_be_negative_for_short_receiving(store):
    store.position_first_seen("BTCUSDT", "SHORT", NOW)
    assert store.add_funding_cost("BTCUSDT", "SHORT", -0.4) == pytest.approx(-0.4)


def test_forget_position_clears_state(store):
    store.position_first_seen("BTCUSDT", "LONG", NOW)
    store.add_funding_cost("BTCUSDT", "LONG", 1.0)
    assert store.seen_positions() == {("BTCUSDT", "LONG")}
    store.forget_position("BTCUSDT", "LONG")
    assert store.seen_positions() == set()
    # 重新出现时应视为新持仓
    assert store.position_first_seen("BTCUSDT", "LONG", NOW + timedelta(hours=1)) == NOW + timedelta(hours=1)
