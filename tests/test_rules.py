"""规则引擎测试：持续性确认、冷却去重、静默期路由、限流、开关与覆盖。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Config, RuleConfig
from src.models import AccountSnapshot, PositionRisk, Severity
from src.rules import (
    ACCOUNT_KEY,
    RuleEngine,
    evaluate_position_rules,
    evaluate_heartbeat_rule,
)
from src.store import Store

SGT = timezone(timedelta(hours=8))


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.quiet_start = datetime(2000, 1, 1, 23, 30).time()
    cfg.quiet_end = datetime(2000, 1, 1, 7, 0).time()
    cfg.max_per_hour = 12
    cfg.thresholds = {
        "liq_to_stop_min_ratio": 3.0,
        "margin_ratio_warn": 0.60,
        "margin_ratio_critical": 0.80,
        "funding_cost_r_warn": 0.30,
        "portfolio_heat_max_pct": 4.0,
        "daily_loss_warn_pct": 2.0,
        "daily_loss_critical_pct": 3.0,
        "drawdown_stop_pct": 20.0,
        "assumed_mmr": 0.005,
    }
    cfg.symbols = {"BTCUSDT": 2.0}
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def make_position(**kw) -> PositionRisk:
    base = dict(
        symbol="BTCUSDT",
        side="LONG",
        position_amt=0.1,
        entry_price=60000.0,
        mark_price=60000.0,
        notional=6000.0,
        unrealized_pnl=0.0,
        leverage=10.0,
        isolated_margin=500.0,
        maint_margin=30.0,
        margin_ratio=0.06,
        liq_price=54000.0,
        liq_distance_pct=0.10,
        planned_stop_pct=0.02,
        r_value_usdt=120.0,
        funding_cost_usdt=0.0,
        funding_rate=0.0001,
    )
    base.update(kw)
    return PositionRisk(**base)


# ---------- 持续性确认 ----------


def test_confirmations_gate_requires_consecutive_true(store):
    cfg = make_cfg()
    cfg.rules = {"margin_ratio_low": RuleConfig("margin_ratio_low", confirmations=3)}
    engine = RuleEngine(cfg, store, SGT)
    snap = AccountSnapshot(
        equity=10000.0, available_balance=10000.0,
        positions=[make_position(margin_ratio=0.65)],
    )
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)  # SGT 14:00

    for _ in range(2):
        assert engine.process(evaluate_position_rules(cfg, snap), now) == []
    decisions = engine.process(evaluate_position_rules(cfg, snap), now)
    titles = [d.alert.rule_id for d in decisions]
    assert "margin_ratio_low" in titles


def test_streak_resets_when_condition_clears(store):
    cfg = make_cfg()
    cfg.rules = {"margin_ratio_low": RuleConfig("margin_ratio_low", confirmations=3)}
    engine = RuleEngine(cfg, store, SGT)
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)

    hot = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.65)])
    cool = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.10)])

    engine.process(evaluate_position_rules(cfg, hot), now)   # 连续 1
    engine.process(evaluate_position_rules(cfg, hot), now)   # 连续 2
    engine.process(evaluate_position_rules(cfg, cool), now)  # 条件消失 -> 清零
    assert engine.process(evaluate_position_rules(cfg, hot), now) == []  # 重新计到 1
    assert engine.process(evaluate_position_rules(cfg, hot), now) == []  # 2
    fired = engine.process(evaluate_position_rules(cfg, hot), now)       # 3 -> 触发
    assert [d.alert.rule_id for d in fired] == ["margin_ratio_low"]


# ---------- 冷却去重 ----------


def test_cooldown_suppresses_repeat(store):
    cfg = make_cfg()
    cfg.rules = {"margin_ratio_low": RuleConfig("margin_ratio_low", confirmations=1)}
    engine = RuleEngine(cfg, store, SGT)
    snap = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.65)])
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)

    assert len(engine.process(evaluate_position_rules(cfg, snap), now)) == 1
    later = now + timedelta(minutes=30)
    assert engine.process(evaluate_position_rules(cfg, snap), later) == []


def test_cooldown_expires(store):
    cfg = make_cfg()
    cfg.rules = {"margin_ratio_low": RuleConfig("margin_ratio_low", confirmations=1)}
    cfg.cooldown_default = 600
    engine = RuleEngine(cfg, store, SGT)
    snap = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.65)])
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)

    assert len(engine.process(evaluate_position_rules(cfg, snap), now)) == 1
    later = now + timedelta(seconds=601)
    assert len(engine.process(evaluate_position_rules(cfg, snap), later)) == 1


# ---------- 静默期与分级 ----------


def test_warn_queued_during_quiet_hours(store):
    cfg = make_cfg()
    cfg.rules = {"margin_ratio_low": RuleConfig("margin_ratio_low", confirmations=1)}
    engine = RuleEngine(cfg, store, SGT)
    snap = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.65)])
    quiet_now = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)  # SGT 04:00

    decisions = engine.process(evaluate_position_rules(cfg, snap), quiet_now)
    warn = [d for d in decisions if d.alert.severity is Severity.WARN]
    assert warn and all(not d.immediate for d in warn)


def test_critical_bypasses_quiet_hours(store):
    cfg = make_cfg()
    cfg.rules = {"liq_distance_insufficient": RuleConfig("liq_distance_insufficient", confirmations=1)}
    engine = RuleEngine(cfg, store, SGT)
    snap = AccountSnapshot(
        10000.0, 10000.0,
        [make_position(liq_distance_pct=0.02, planned_stop_pct=0.02)],  # 1.0x < 3.0x
    )
    quiet_now = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)

    decisions = engine.process(evaluate_position_rules(cfg, snap), quiet_now)
    crit = [d for d in decisions if d.alert.severity is Severity.CRITICAL]
    assert crit and all(d.immediate for d in crit)


# ---------- 限流 ----------


def test_rate_limit_defers_non_critical(store):
    cfg = make_cfg()
    cfg.max_per_hour = 1
    cfg.rules = {"margin_ratio_low": RuleConfig("margin_ratio_low", confirmations=1)}
    engine = RuleEngine(cfg, store, SGT)
    snap = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.65)])
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)

    # 预置一封已发送邮件，占满额度
    from src.models import Alert
    store.insert_alert(
        Alert("x", "x", Severity.WARN, "t", "b", created_at=now), sent_at=now
    )
    decisions = engine.process(evaluate_position_rules(cfg, snap), now)
    assert decisions and all(not d.immediate for d in decisions)


# ---------- 开关与阈值 ----------


def test_disabled_rule_is_skipped(store):
    cfg = make_cfg()
    cfg.rules = {
        "margin_ratio_low": RuleConfig("margin_ratio_low", enabled=False, confirmations=1)
    }
    engine = RuleEngine(cfg, store, SGT)
    snap = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.65)])
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
    assert engine.process(evaluate_position_rules(cfg, snap), now) == []


def test_severity_escalates_with_margin_ratio(store):
    cfg = make_cfg()
    snap_warn = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.65)])
    snap_crit = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.85)])
    results_warn = [r for r in evaluate_position_rules(cfg, snap_warn) if r.rule_id == "margin_ratio_low"]
    results_crit = [r for r in evaluate_position_rules(cfg, snap_crit) if r.rule_id == "margin_ratio_low"]
    assert results_warn[0].severity is Severity.WARN
    assert results_crit[0].severity is Severity.CRITICAL


def test_dynamic_severity_survives_engine(store):
    """回归测试：引擎不得用 DEFAULTS 的静态分级覆盖规则计算出的动态分级。"""
    cfg = make_cfg()
    cfg.rules = {"margin_ratio_low": RuleConfig("margin_ratio_low", confirmations=1)}
    engine = RuleEngine(cfg, store, SGT)
    snap = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.85)])
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)

    decisions = engine.process(evaluate_position_rules(cfg, snap), now)
    margin = [d for d in decisions if d.alert.rule_id == "margin_ratio_low"]
    assert margin and margin[0].alert.severity is Severity.CRITICAL


def test_explicit_config_severity_overrides_computed(store):
    """config.yaml 显式配置的分级优先级最高。"""
    cfg = make_cfg()
    cfg.rules = {
        "margin_ratio_low": RuleConfig(
            "margin_ratio_low", severity="WARN", confirmations=1
        )
    }
    engine = RuleEngine(cfg, store, SGT)
    snap = AccountSnapshot(10000.0, 10000.0, [make_position(margin_ratio=0.85)])
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)

    decisions = engine.process(evaluate_position_rules(cfg, snap), now)
    margin = [d for d in decisions if d.alert.rule_id == "margin_ratio_low"]
    assert margin and margin[0].alert.severity is Severity.WARN


def test_account_level_rules_use_account_key(store):
    cfg = make_cfg()
    snap = AccountSnapshot(
        equity=10000.0, available_balance=1000.0, positions=[],
        daily_loss_pct=2.5, drawdown_pct=25.0,
    )
    results = {r.rule_id: r for r in evaluate_position_rules(cfg, snap)}
    assert results["daily_loss_limit"].triggered
    assert results["daily_loss_limit"].dedup_key == ACCOUNT_KEY
    assert results["drawdown_stop"].triggered
    assert results["drawdown_stop"].severity is Severity.CRITICAL
    assert not results["portfolio_heat_exceeded"].triggered


def test_liq_rule_skipped_without_planned_stop(store):
    cfg = make_cfg()
    snap = AccountSnapshot(10000.0, 10000.0, [make_position(planned_stop_pct=None)])
    result = [r for r in evaluate_position_rules(cfg, snap) if r.rule_id == "liq_distance_insufficient"][0]
    assert not result.triggered
    assert "未配置" in result.body


# ---------- 心跳 ----------


def test_heartbeat_triggers_when_stale(store):
    cfg = make_cfg()
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
    store.heartbeat_ok("positions", now - timedelta(seconds=400))
    result = evaluate_heartbeat_rule(cfg, store, now, SGT, "positions")
    assert result.triggered
    assert result.severity is Severity.CRITICAL


def test_heartbeat_ok_when_fresh(store):
    cfg = make_cfg()
    now = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
    store.heartbeat_ok("positions", now - timedelta(seconds=30))
    assert not evaluate_heartbeat_rule(cfg, store, now, SGT, "positions").triggered
