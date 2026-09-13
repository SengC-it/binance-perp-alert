"""持仓风险计算测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Config
from src.position_engine import build_position_risk, build_snapshot
from src.store import Store

SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)

RAW_LONG = {
    "symbol": "BTCUSDT",
    "positionSide": "BOTH",
    "positionAmt": "0.1",
    "entryPrice": "60000",
    "markPrice": "61000",
    "unRealizedProfit": "100",
    "liquidationPrice": "54300",
    "leverage": "10",
    "isolatedMargin": "500",
    "isolatedWallet": "500",
}


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "pos.db")
    yield s
    s.close()


@pytest.fixture()
def cfg():
    c = Config()
    c.symbols = {"BTCUSDT": 2.0}
    c.thresholds = {"assumed_mmr": 0.005}
    return c


def test_position_math(store, cfg):
    risk = build_position_risk(
        RAW_LONG, maint_margin_raw=30.5, funding_rate=0.0001,
        mark_price_raw=61000.0, cfg=cfg, store=store, now=NOW, elapsed_hours=8.0,
    )
    assert risk is not None
    assert risk.side == "LONG"
    assert risk.notional == pytest.approx(6100.0)
    assert risk.margin_ratio == pytest.approx(0.061)
    assert risk.margin_ratio_estimated is False
    assert risk.liq_distance_pct == pytest.approx(6700 / 61000)
    assert risk.r_value_usdt == pytest.approx(120.0)
    assert risk.liq_to_stop_ratio == pytest.approx((6700 / 61000) / 0.02)
    assert risk.funding_cost_usdt == pytest.approx(0.61)


def test_zero_position_returns_none(store, cfg):
    raw = dict(RAW_LONG, positionAmt="0")
    assert build_position_risk(
        raw, None, 0.0, None, cfg, store, NOW, 0.0
    ) is None


def test_maint_margin_estimated_when_v3_missing(store, cfg):
    risk = build_position_risk(
        RAW_LONG, maint_margin_raw=None, funding_rate=0.0,
        mark_price_raw=None, cfg=cfg, store=store, now=NOW, elapsed_hours=0.0,
    )
    assert risk is not None
    assert risk.margin_ratio_estimated is True
    assert risk.maint_margin == pytest.approx(6100 * 0.005)


def test_short_position_receives_funding(store, cfg):
    raw = dict(RAW_LONG, positionAmt="-0.1")
    risk = build_position_risk(
        raw, 30.5, 0.0001, 61000.0, cfg, store, NOW, elapsed_hours=8.0
    )
    assert risk is not None
    assert risk.side == "SHORT"
    assert risk.funding_cost_usdt == pytest.approx(-0.61)


def test_funding_scales_with_elapsed_intervals(store, cfg):
    risk = build_position_risk(
        RAW_LONG, 30.5, 0.0001, 61000.0, cfg, store, NOW, elapsed_hours=24.0
    )
    assert risk is not None
    assert risk.funding_cost_usdt == pytest.approx(0.61 * 3)


def test_no_planned_stop_means_zero_r(store, cfg):
    cfg.symbols = {}
    risk = build_position_risk(
        RAW_LONG, 30.5, 0.0, 61000.0, cfg, store, NOW, elapsed_hours=0.0
    )
    assert risk is not None
    assert risk.r_value_usdt == 0.0
    assert risk.liq_to_stop_ratio is None


def test_snapshot_equity_and_daily_loss(store, cfg):
    account = {"totalMarginBalance": "10000", "availableBalance": "8000"}
    premium = {"BTCUSDT": {"markPrice": "61000", "lastFundingRate": "0.0001"}}
    v3 = [{"symbol": "BTCUSDT", "positionSide": "BOTH", "maintMargin": "30.5"}]

    first = build_snapshot(account, [RAW_LONG], v3, premium, cfg, store, NOW, SGT)
    assert first.equity == pytest.approx(10000.0)
    assert len(first.positions) == 1
    assert first.daily_loss_pct == 0.0
    assert first.drawdown_pct == 0.0

    later = NOW + timedelta(minutes=30)
    second = build_snapshot(
        {"totalMarginBalance": "9500", "availableBalance": "8000"},
        [RAW_LONG], v3, premium, cfg, store, later, SGT,
    )
    assert second.daily_loss_pct == pytest.approx(5.0)
    assert second.drawdown_pct == pytest.approx(5.0)


def test_portfolio_heat_sums_risk(store, cfg):
    account = {"totalMarginBalance": "10000", "availableBalance": "8000"}
    premium = {"BTCUSDT": {"markPrice": "61000", "lastFundingRate": "0"}}
    snap = build_snapshot(account, [RAW_LONG], None, premium, cfg, store, NOW, SGT)
    # 1R = 120 USDT，权益 10000 -> 1.2%
    assert snap.portfolio_heat_pct == pytest.approx(1.2)


def test_closed_position_state_is_cleaned(store, cfg):
    account = {"totalMarginBalance": "10000", "availableBalance": "8000"}
    premium = {"BTCUSDT": {"markPrice": "61000", "lastFundingRate": "0"}}
    build_snapshot(account, [RAW_LONG], None, premium, cfg, store, NOW, SGT)
    assert store.seen_positions() == {("BTCUSDT", "LONG")}

    build_snapshot(account, [], None, premium, cfg, store, NOW + timedelta(minutes=1), SGT)
    assert store.seen_positions() == set()


def test_mark_price_prefers_premium_index(store, cfg):
    account = {"totalMarginBalance": "10000", "availableBalance": "8000"}
    premium = {"BTCUSDT": {"markPrice": "62000", "lastFundingRate": "0"}}
    snap = build_snapshot(account, [RAW_LONG], None, premium, cfg, store, NOW, SGT)
    assert snap.positions[0].mark_price == pytest.approx(62000.0)
    assert snap.positions[0].notional == pytest.approx(6200.0)
