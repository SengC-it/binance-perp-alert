"""前向验证与滚动对账的测试。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.config import Config
from src.directional_engine import SymbolVol, build_signal
from src.forward_check import (
    is_due,
    leg_funding_pct,
    leg_price_return_pct,
    portfolio_return_pct,
    record_signal,
    render_reconciliation,
    verify_one,
    verify_pending,
)
from src.store import Store

NOW = datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def make_vol(symbol: str, vol: float, price: float = 100.0) -> SymbolVol:
    return SymbolVol(
        symbol=symbol,
        realized_vol_pct=vol,
        last_price=price,
        return_1d_pct=0.0,
        return_window_pct=0.0,
        quote_volume_24h=8e7,
        days_used=30,
    )


def make_signal(k_long=2, k_short=2, n=10, as_of="2026-09-13"):
    vols = [make_vol(f"S{i}", 10.0 * (i + 1), 100.0 + i) for i in range(n)]
    return build_signal(vols, k_long, k_short, 30, as_of)


# ---------------------------------------------------------------------------
# 纯计算
# ---------------------------------------------------------------------------

def test_leg_price_return_long():
    assert leg_price_return_pct(100.0, 110.0, 1) == pytest.approx(10.0)


def test_leg_price_return_short_inverts():
    """做空时价格下跌应该赚钱。"""
    assert leg_price_return_pct(100.0, 90.0, -1) == pytest.approx(10.0)


def test_leg_price_return_short_loses_on_rally():
    assert leg_price_return_pct(100.0, 120.0, -1) == pytest.approx(-20.0)


def test_leg_price_return_guards_zero_entry():
    assert leg_price_return_pct(0.0, 110.0, 1) == 0.0


def test_funding_long_pays_when_rate_positive():
    """费率为正时，多头付出。"""
    assert leg_funding_pct(0.001, 1) == pytest.approx(-0.1)


def test_funding_short_receives_when_rate_positive():
    assert leg_funding_pct(0.001, -1) == pytest.approx(0.1)


def test_portfolio_return_is_equal_weighted_and_charged():
    # 多头 +10%、空头 +10%，各自收到 0 资金费，成本 0.16%
    net, long_net, short_net = portfolio_return_pct(
        [10.0], [10.0], [0.0], [0.0], cost_pct=0.16
    )
    assert long_net == pytest.approx(9.84)
    assert short_net == pytest.approx(9.84)
    assert net == pytest.approx(9.84)


def test_portfolio_return_averages_within_leg():
    net, _, _ = portfolio_return_pct(
        [10.0, 20.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], cost_pct=0.0
    )
    # 多头腿均值 15%，空头腿 0%，等权 -> 7.5%
    assert net == pytest.approx(7.5)


def test_portfolio_return_empty_leg_returns_zero():
    assert portfolio_return_pct([], [1.0], [], [], 0.1) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# 到期判断
# ---------------------------------------------------------------------------

def test_is_due_true_after_horizon():
    trade = {"signal_date": "2026-01-01", "horizon_days": 30}
    assert is_due(trade, date(2026, 1, 31)) is True


def test_is_due_false_before_horizon():
    trade = {"signal_date": "2026-01-01", "horizon_days": 30}
    assert is_due(trade, date(2026, 1, 20)) is False


def test_is_due_handles_bad_date():
    assert is_due({"signal_date": "not-a-date", "horizon_days": 30}, date(2026, 1, 31)) is False


# ---------------------------------------------------------------------------
# 登记
# ---------------------------------------------------------------------------

def test_record_signal_persists_entry_prices(store):
    sig = make_signal()
    trade_id = record_signal(store, sig, Config())
    assert trade_id is not None
    rows = store.paper_trades()
    assert len(rows) == 1
    entry = json.loads(rows[0]["entry_prices"])
    assert set(entry) == {v.symbol for v in sig.longs + sig.shorts}
    assert rows[0]["status"] == "pending"


def test_record_signal_is_idempotent(store):
    sig = make_signal()
    assert record_signal(store, sig, Config()) is not None
    assert record_signal(store, sig, Config()) is None, "同一信号日不应重复登记"
    assert len(store.paper_trades()) == 1


def test_record_signal_skips_incomplete_portfolio(store):
    sig = make_signal(k_long=5, k_short=5, n=4)   # 标的不足，两条腿都为空
    assert record_signal(store, sig, Config()) is None
    assert store.paper_trades() == []


# ---------------------------------------------------------------------------
# 单条验证
# ---------------------------------------------------------------------------

def test_verify_one_computes_net_return(store):
    sig = make_signal(k_long=2, k_short=2)
    record_signal(store, sig, Config())
    trade = store.paper_trades()[0]

    longs = json.loads(trade["longs_json"])
    shorts = json.loads(trade["shorts_json"])
    entry = json.loads(trade["entry_prices"])
    # 出场价相对**登记的入场价**构造：多头 +10%，空头 -10%（做空赚 10%）
    prices = {s: (entry[s], entry[s] * 1.10) for s in longs}
    prices.update({s: (entry[s], entry[s] * 0.90) for s in shorts})

    outcome = verify_one(trade, prices, {}, cost_pct=0.16)
    assert outcome is not None
    net, long_net, short_net = outcome
    assert long_net == pytest.approx(10.0 - 0.16, rel=1e-6)
    assert short_net == pytest.approx(10.0 - 0.16, rel=1e-6)
    assert net == pytest.approx(10.0 - 0.16, rel=1e-6)


def test_verify_one_applies_funding_sign(store):
    sig = make_signal(k_long=1, k_short=1)
    record_signal(store, sig, Config())
    trade = store.paper_trades()[0]
    longs = json.loads(trade["longs_json"])
    shorts = json.loads(trade["shorts_json"])
    entry = json.loads(trade["entry_prices"])
    prices = {s: (entry[s], entry[s]) for s in longs + shorts}   # 价格不动
    # 费率为正：多头付 0.1%，空头收 0.1%
    funding = {s: 0.001 for s in longs + shorts}

    net, long_net, short_net = verify_one(trade, prices, funding, cost_pct=0.0)
    assert long_net == pytest.approx(-0.1)
    assert short_net == pytest.approx(+0.1)
    assert net == pytest.approx(0.0)


def test_verify_one_returns_none_on_missing_prices(store):
    sig = make_signal()
    record_signal(store, sig, Config())
    trade = store.paper_trades()[0]
    assert verify_one(trade, {}, {}, 0.16) is None


# ---------------------------------------------------------------------------
# 批量验证
# ---------------------------------------------------------------------------

class FakeClient:
    """假行情客户端：所有标的价格固定按 map 返回。"""

    def __init__(self, exit_price: float = 110.0, fail: bool = False):
        self.exit_price = exit_price
        self.fail = fail
        self.klines_calls = 0

    def klines(self, symbol, interval="1d", limit=60):
        if self.fail:
            raise RuntimeError("模拟行情失败")
        self.klines_calls += 1
        return [[0, "100", "100", "100", "100"], [1, "100", "100", "100", str(self.exit_price)]]

    def funding_history(self, symbol, start_ms, limit=200):
        return [{"fundingRate": "0.0001"}]


def test_verify_pending_backfills_due_trade(store):
    sig = make_signal(as_of="2026-01-01")
    record_signal(store, sig, Config())
    stats = verify_pending(store, Config(), FakeClient(), today=date(2026, 3, 1))
    assert stats["verified"] == 1
    rows = store.paper_trades()
    assert rows[0]["status"] == "verified"
    assert rows[0]["net_return_pct"] is not None


def test_verify_pending_skips_not_due(store):
    sig = make_signal(as_of="2026-01-01")
    record_signal(store, sig, Config())
    stats = verify_pending(store, Config(), FakeClient(), today=date(2026, 1, 5))
    assert stats["verified"] == 0
    assert store.paper_trades()[0]["status"] == "pending"


def test_verify_pending_marks_failure_without_losing_record(store):
    sig = make_signal(as_of="2026-01-01")
    record_signal(store, sig, Config())
    stats = verify_pending(store, Config(), FakeClient(fail=True), today=date(2026, 3, 1))
    assert stats["failed"] == 1
    row = store.paper_trades()[0]
    assert row["status"] == "failed"
    assert "模拟行情失败" in row["error"]


# ---------------------------------------------------------------------------
# 对账统计与渲染
# ---------------------------------------------------------------------------

def test_paper_stats_counts_states(store):
    sig = make_signal(as_of="2026-01-01")
    record_signal(store, sig, Config())
    verify_pending(store, Config(), FakeClient(), today=date(2026, 3, 1))
    stats = store.paper_stats()
    assert stats["total"] == 1
    assert stats["verified"] == 1
    assert stats["pending"] == 0


def test_render_reconciliation_without_samples(store):
    text = render_reconciliation(store)
    assert "还没有可核对的记录" in text
    assert "未被反驳的假设" in text


def test_render_reconciliation_warns_on_small_sample(store):
    """样本量不足时必须明说，不能拿个位数样本下结论。"""
    sig = make_signal(as_of="2026-01-01")
    record_signal(store, sig, Config())
    verify_pending(store, Config(), FakeClient(), today=date(2026, 3, 1))
    text = render_reconciliation(store)
    assert "还不足以下任何结论" in text
    assert "独立事件" in text


def _seed_verified(store, n: int, net: float) -> None:
    """直接塞入 n 条已核对记录，用于测试对账判读。"""
    for i in range(n):
        tid = store.insert_paper_trade(
            strategy="xs_lowvol",
            signal_date=f"2026-01-{i + 1:02d}",
            horizon_days=30,
            lookback_days=30,
            k_long=2,
            k_short=2,
            longs=["AUSDT", "BUSDT"],
            shorts=["YUSDT", "ZUSDT"],
            entry_prices={"AUSDT": 1.0, "BUSDT": 2.0, "YUSDT": 3.0, "ZUSDT": 4.0},
        )
        assert tid is not None
        store.mark_paper_verified(tid, NOW, net, net, net)


def test_render_reconciliation_flags_negative_result(store):
    """前向验证为负时应该建议暂停，而不是给策略找解释。"""
    _seed_verified(store, 12, -1.5)
    text = render_reconciliation(store)
    assert "与回测结论冲突" in text
    assert "找解释" in text


def test_render_reconciliation_flags_low_return(store):
    """样本够但收益远低于回测时，应提醒不要放大仓位。"""
    _seed_verified(store, 12, 0.4)
    text = render_reconciliation(store)
    assert "远低于回测水平" in text
    assert "不要放大仓位" in text


def test_render_reconciliation_confirms_when_consistent(store):
    _seed_verified(store, 12, 3.0)
    text = render_reconciliation(store)
    assert "方向一致" in text
