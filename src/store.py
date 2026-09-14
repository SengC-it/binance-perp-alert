"""SQLite 状态层。

承担四件事：
  1. 规则状态：持续性确认的连续计数、上次触发时间（用于冷却去重）
  2. 告警记录：已发 / 待发（每日摘要队列）
  3. 心跳：各组件最近一次正常时间
  4. 权益快照与持仓首见时间：用于日亏损、回撤、资金费累计估算
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Alert, Severity

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_state (
    rule_id       TEXT NOT NULL,
    dedup_key     TEXT NOT NULL,
    streak        INTEGER NOT NULL DEFAULT 0,
    last_eval_at  TEXT,
    last_fired_at TEXT,
    PRIMARY KEY (rule_id, dedup_key)
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id     TEXT NOT NULL,
    dedup_key   TEXT NOT NULL,
    symbol      TEXT NOT NULL DEFAULT '-',
    severity    TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    sent_at     TEXT,
    send_error  TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_pending ON alerts (sent_at, severity);

CREATE TABLE IF NOT EXISTS heartbeat (
    component  TEXT PRIMARY KEY,
    last_ok_at TEXT NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts     TEXT PRIMARY KEY,
    equity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions_seen (
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    funding_cost_usdt REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, side)
);

-- 前向验证（paper trading）。
-- 每条方向性信号都落一条记录，到期后用真实行情回填实际结果。
-- 目的是让系统能自己持续证明或证伪，而不是靠一次回测下结论。
CREATE TABLE IF NOT EXISTS paper_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT NOT NULL,
    strategy_id   TEXT NOT NULL DEFAULT '',
    spec_hash     TEXT NOT NULL DEFAULT '',
    variant       TEXT NOT NULL DEFAULT '',
    signal_date   TEXT NOT NULL,
    signal_timestamp TEXT,
    execution_date TEXT,
    execution_lag_days INTEGER NOT NULL DEFAULT 1,
    horizon_days  INTEGER NOT NULL,
    lookback_days INTEGER NOT NULL,
    k_long        INTEGER NOT NULL,
    k_short       INTEGER NOT NULL,
    longs_json    TEXT NOT NULL,
    shorts_json   TEXT NOT NULL,
    entry_prices  TEXT NOT NULL,          -- JSON: {symbol: price}
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending / verified / failed
    verified_at   TEXT,
    net_return_pct REAL,
    long_return_pct  REAL,
    short_return_pct REAL,
    error         TEXT,
    UNIQUE (strategy, signal_date, horizon_days)
);

-- XS-LOWVOL 周度 PAPER portfolio 的连续 NAV 与换仓审计。
CREATE TABLE IF NOT EXISTS paper_nav (
    strategy_id    TEXT NOT NULL,
    spec_hash      TEXT NOT NULL,
    day            TEXT NOT NULL,
    nav            REAL NOT NULL,
    daily_pnl      REAL NOT NULL,
    long_pnl       REAL NOT NULL,
    short_pnl      REAL NOT NULL,
    funding_pnl    REAL NOT NULL,
    cost_pnl       REAL NOT NULL,
    rebalance      INTEGER NOT NULL DEFAULT 0,
    changed_json   TEXT NOT NULL DEFAULT '[]',
    resized_json   TEXT NOT NULL DEFAULT '[]',
    turnover_notional REAL NOT NULL DEFAULT 0,
    positions_json TEXT NOT NULL DEFAULT '{}',
    position_notionals_json TEXT NOT NULL DEFAULT '{}',
    targets_json   TEXT NOT NULL DEFAULT '{}',
    scale          REAL NOT NULL DEFAULT 1,
    PRIMARY KEY (strategy_id, day)
);

CREATE TABLE IF NOT EXISTS paper_rebalances (
    strategy_id    TEXT NOT NULL,
    spec_hash      TEXT NOT NULL,
    rebalance_date TEXT NOT NULL,
    signal_date    TEXT NOT NULL,
    execution_date TEXT NOT NULL,
    targets_json   TEXT NOT NULL,
    changed_json   TEXT NOT NULL DEFAULT '[]',
    resized_json   TEXT NOT NULL DEFAULT '[]',
    turnover_notional REAL NOT NULL DEFAULT 0,
    scale          REAL NOT NULL DEFAULT 1,
    PRIMARY KEY (strategy_id, rebalance_date)
);

CREATE TABLE IF NOT EXISTS paper_portfolios (
    strategy_id TEXT PRIMARY KEY,
    spec_hash   TEXT NOT NULL,
    last_day    TEXT,
    state_json  TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL
);
"""


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).isoformat(timespec="seconds")


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text)


class Store:
    """所有状态读写都经过这里。线程安全（单进程内加锁）。"""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate_paper_columns()
        self._conn.commit()

    def _migrate_paper_columns(self) -> None:
        """为旧数据库补齐策略身份、执行时间和 resize 审计字段。"""
        def columns_for(table: str) -> set[str]:
            return {
                str(row[1]) for row in self._conn.execute(f"PRAGMA table_info({table})")
            }

        additions = {
            "strategy_id": "TEXT NOT NULL DEFAULT ''",
            "spec_hash": "TEXT NOT NULL DEFAULT ''",
            "variant": "TEXT NOT NULL DEFAULT ''",
            "signal_timestamp": "TEXT",
            "execution_date": "TEXT",
            "execution_lag_days": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, definition in additions.items():
            if name not in columns_for("paper_trades"):
                self._conn.execute(
                    f"ALTER TABLE paper_trades ADD COLUMN {name} {definition}"
                )
        for table, fields in {
            "paper_nav": {
                "resized_json": "TEXT NOT NULL DEFAULT '[]'",
                "turnover_notional": "REAL NOT NULL DEFAULT 0",
                "position_notionals_json": "TEXT NOT NULL DEFAULT '{}'",
                "targets_json": "TEXT NOT NULL DEFAULT '{}'",
                "scale": "REAL NOT NULL DEFAULT 1",
            },
            "paper_rebalances": {
                "resized_json": "TEXT NOT NULL DEFAULT '[]'",
                "turnover_notional": "REAL NOT NULL DEFAULT 0",
                "scale": "REAL NOT NULL DEFAULT 1",
            },
        }.items():
            columns = columns_for(table)
            for name, definition in fields.items():
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 规则状态 ----------

    def bump_streak(self, rule_id: str, dedup_key: str, triggered: bool, now: datetime) -> int:
        """触发则连续计数 +1，未触发则清零。返回新的连续次数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT streak FROM rule_state WHERE rule_id=? AND dedup_key=?",
                (rule_id, dedup_key),
            ).fetchone()
            streak = (row["streak"] if row else 0)
            streak = streak + 1 if triggered else 0
            self._conn.execute(
                """
                INSERT INTO rule_state (rule_id, dedup_key, streak, last_eval_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(rule_id, dedup_key) DO UPDATE SET
                    streak = excluded.streak,
                    last_eval_at = excluded.last_eval_at
                """,
                (rule_id, dedup_key, streak, _iso(now)),
            )
            self._conn.commit()
            return streak

    def last_fired_at(self, rule_id: str, dedup_key: str) -> datetime | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_fired_at FROM rule_state WHERE rule_id=? AND dedup_key=?",
                (rule_id, dedup_key),
            ).fetchone()
        return _parse(row["last_fired_at"]) if row else None

    def mark_fired(self, rule_id: str, dedup_key: str, now: datetime) -> None:
        """记录触发并清零连续计数，使下次告警需要重新确认。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rule_state (rule_id, dedup_key, streak, last_fired_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(rule_id, dedup_key) DO UPDATE SET
                    streak = 0,
                    last_fired_at = excluded.last_fired_at
                """,
                (rule_id, dedup_key, _iso(now)),
            )
            self._conn.commit()

    # ---------- 告警 ----------

    def insert_alert(self, alert: Alert, sent_at: datetime | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO alerts
                    (rule_id, dedup_key, symbol, severity, title, body, created_at, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.rule_id,
                    alert.dedup_key,
                    alert.symbol,
                    alert.severity.value,
                    alert.title,
                    alert.body,
                    _iso(alert.created_at or datetime.now(timezone.utc)),
                    _iso(sent_at) if sent_at else None,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def pending_alerts(self, limit: int = 200) -> list[Alert]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE sent_at IS NULL ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Alert(
                alert_id=r["id"],
                rule_id=r["rule_id"],
                dedup_key=r["dedup_key"],
                symbol=r["symbol"],
                severity=Severity(r["severity"]),
                title=r["title"],
                body=r["body"],
                created_at=_parse(r["created_at"]),
            )
            for r in rows
        ]

    def mark_sent(self, alert_id: int, now: datetime, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET sent_at=?, send_error=? WHERE id=?",
                (_iso(now), error, alert_id),
            )
            self._conn.commit()

    def sent_count_since(self, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE sent_at IS NOT NULL AND sent_at >= ?",
                (_iso(since),),
            ).fetchone()
        return int(row["n"])

    # ---------- 心跳 ----------

    def heartbeat_ok(self, component: str, now: datetime, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO heartbeat (component, last_ok_at, detail) VALUES (?, ?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    last_ok_at = excluded.last_ok_at,
                    detail = excluded.detail
                """,
                (component, _iso(now), detail),
            )
            self._conn.commit()

    def heartbeat_last_ok(self, component: str) -> datetime | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_ok_at FROM heartbeat WHERE component=?", (component,)
            ).fetchone()
        return _parse(row["last_ok_at"]) if row else None

    # ---------- 权益与持仓 ----------

    def record_equity(self, now: datetime, equity: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity_snapshots (ts, equity) VALUES (?, ?)",
                (_iso(now), float(equity)),
            )
            self._conn.commit()

    def first_equity_since(self, since: datetime) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT equity FROM equity_snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
                (_iso(since),),
            ).fetchone()
        return float(row["equity"]) if row else None

    def peak_equity(self, since: datetime | None = None) -> float | None:
        with self._lock:
            if since is None:
                row = self._conn.execute(
                    "SELECT MAX(equity) AS e FROM equity_snapshots"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT MAX(equity) AS e FROM equity_snapshots WHERE ts >= ?",
                    (_iso(since),),
                ).fetchone()
        return float(row["e"]) if row and row["e"] is not None else None

    def position_first_seen(self, symbol: str, side: str, now: datetime) -> datetime:
        """返回该持仓首次被观测到的时间；不存在则写入当前时间。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT first_seen_at FROM positions_seen WHERE symbol=? AND side=?",
                (symbol, side),
            ).fetchone()
            if row:
                return _parse(row["first_seen_at"])  # type: ignore[return-value]
            self._conn.execute(
                "INSERT INTO positions_seen (symbol, side, first_seen_at) VALUES (?, ?, ?)",
                (symbol, side, _iso(now)),
            )
            self._conn.commit()
        return _utc(now)

    def add_funding_cost(self, symbol: str, side: str, delta_usdt: float) -> float:
        """累加资金费估算，返回累计值。"""
        with self._lock:
            self._conn.execute(
                "UPDATE positions_seen SET funding_cost_usdt = funding_cost_usdt + ? "
                "WHERE symbol=? AND side=?",
                (float(delta_usdt), symbol, side),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT funding_cost_usdt FROM positions_seen WHERE symbol=? AND side=?",
                (symbol, side),
            ).fetchone()
        return float(row["funding_cost_usdt"]) if row else 0.0

    def forget_position(self, symbol: str, side: str) -> None:
        """持仓已平掉时清理累计状态。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM positions_seen WHERE symbol=? AND side=?", (symbol, side)
            )
            self._conn.commit()

    def seen_positions(self) -> set[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, side FROM positions_seen"
            ).fetchall()
        return {(r["symbol"], r["side"]) for r in rows}

    # ---------- 前向验证（paper trading） ----------

    def insert_paper_trade(
        self,
        strategy: str,
        signal_date: str,
        horizon_days: int,
        lookback_days: int,
        k_long: int,
        k_short: int,
        longs: list[str],
        shorts: list[str],
        entry_prices: dict[str, float],
        *,
        strategy_id: str | None = None,
        spec_hash: str = "",
        variant: str = "",
        signal_timestamp: str | None = None,
        execution_date: str | None = None,
        execution_lag_days: int = 1,
    ) -> int | None:
        """登记一条待验证信号，重复 key 时返回 None。"""
        strategy_id = strategy_id or strategy
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO paper_trades
                        (strategy, strategy_id, spec_hash, variant, signal_date,
                         signal_timestamp, execution_date, execution_lag_days,
                         horizon_days, lookback_days, k_long, k_short,
                         longs_json, shorts_json, entry_prices)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        strategy,
                        strategy_id,
                        spec_hash,
                        variant,
                        signal_date,
                        signal_timestamp,
                        execution_date,
                        execution_lag_days,
                        horizon_days,
                        lookback_days,
                        k_long,
                        k_short,
                        json.dumps(longs, ensure_ascii=False),
                        json.dumps(shorts, ensure_ascii=False),
                        json.dumps(entry_prices, ensure_ascii=False),
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def pending_paper_trades(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_trades WHERE status='pending' ORDER BY signal_date ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_paper_verified(
        self,
        trade_id: int,
        now: datetime,
        net_return_pct: float,
        long_return_pct: float,
        short_return_pct: float,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE paper_trades
                   SET status='verified', verified_at=?, net_return_pct=?,
                       long_return_pct=?, short_return_pct=?, error=NULL
                 WHERE id=?
                """,
                (
                    _iso(now),
                    net_return_pct,
                    long_return_pct,
                    short_return_pct,
                    trade_id,
                ),
            )
            self._conn.commit()

    def mark_paper_failed(self, trade_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE paper_trades SET status='failed', error=? WHERE id=?",
                (error[:500], trade_id),
            )
            self._conn.commit()

    def paper_trades(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM paper_trades WHERE status=? ORDER BY signal_date ASC",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM paper_trades ORDER BY signal_date ASC"
                ).fetchall()
        return [dict(r) for r in rows]

    def update_paper_entry_prices(
        self,
        strategy_id: str,
        execution_date: str,
        entry_prices: dict[str, float],
    ) -> None:
        """在完成 execution candle 后，把登记快照替换为实际入场价。"""
        with self._lock:
            self._conn.execute(
                """
                UPDATE paper_trades
                   SET entry_prices=?
                 WHERE strategy_id=? AND execution_date=?
                """,
                (
                    json.dumps(entry_prices, ensure_ascii=False, sort_keys=True),
                    strategy_id,
                    str(execution_date)[:10],
                ),
            )
            self._conn.commit()

    def paper_stats(
        self,
        strategy_id: str | None = None,
        spec_hash: str | None = None,
    ) -> dict[str, Any]:
        """滚动对账统计，可按版本身份隔离。"""
        filters = ["1=1"]
        params: list[Any] = []
        if strategy_id is not None:
            filters.append("strategy_id=?")
            params.append(strategy_id)
        if spec_hash is not None:
            filters.append("spec_hash=?")
            params.append(spec_hash)
        where = " AND ".join(filters)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='verified' THEN 1 ELSE 0 END) AS verified,
                       SUM(CASE WHEN status='pending'  THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN status='failed'   THEN 1 ELSE 0 END) AS failed
                  FROM paper_trades
                 WHERE {where}
                """,
                params,
            ).fetchone()
            rets = [
                float(r["net_return_pct"])
                for r in self._conn.execute(
                    f"SELECT net_return_pct FROM paper_trades "
                    f"WHERE {where} AND status='verified'",
                    params,
                ).fetchall()
                if r["net_return_pct"] is not None
            ]
        wins = sum(1 for r in rets if r > 0)
        return {
            "total": int(row["total"] or 0),
            "verified": int(row["verified"] or 0),
            "pending": int(row["pending"] or 0),
            "failed": int(row["failed"] or 0),
            "mean_return_pct": (sum(rets) / len(rets)) if rets else 0.0,
            "win_rate_pct": (wins / len(rets) * 100.0) if rets else 0.0,
            "best_pct": max(rets) if rets else 0.0,
            "worst_pct": min(rets) if rets else 0.0,
        }

    # ---------- 周度 PAPER portfolio ledger ----------

    def record_paper_nav(
        self,
        strategy_id: str,
        spec_hash: str,
        day: str,
        nav: float,
        daily_pnl: float,
        long_pnl: float,
        short_pnl: float,
        funding_pnl: float,
        cost_pnl: float,
        rebalance: bool,
        changed_symbols: list[str],
        positions: dict[str, int],
        resized_symbols: list[str] | None = None,
        turnover_notional: float = 0.0,
        position_notionals: dict[str, float] | None = None,
        targets: dict[str, int] | None = None,
        scale: float = 1.0,
    ) -> None:
        """按 (strategy, completed day) 幂等写入连续 NAV。"""
        resized_symbols = resized_symbols or []
        position_notionals = position_notionals or {}
        targets = targets or {}
        with self._lock:
            existing = self._conn.execute(
                "SELECT spec_hash FROM paper_nav WHERE strategy_id=? AND day=?",
                (strategy_id, str(day)[:10]),
            ).fetchone()
            if existing is not None and existing["spec_hash"] != spec_hash:
                raise ValueError("同一 PAPER strategy_id 不能混用不同 spec hash")
            self._conn.execute(
                """
                INSERT INTO paper_nav
                    (strategy_id, spec_hash, day, nav, daily_pnl, long_pnl,
                     short_pnl, funding_pnl, cost_pnl, rebalance,
                     changed_json, resized_json, turnover_notional,
                     positions_json, position_notionals_json, targets_json, scale)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id, day) DO UPDATE SET
                    spec_hash=excluded.spec_hash,
                    nav=excluded.nav,
                    daily_pnl=excluded.daily_pnl,
                    long_pnl=excluded.long_pnl,
                    short_pnl=excluded.short_pnl,
                    funding_pnl=excluded.funding_pnl,
                    cost_pnl=excluded.cost_pnl,
                    rebalance=excluded.rebalance,
                    changed_json=excluded.changed_json,
                    resized_json=excluded.resized_json,
                    turnover_notional=excluded.turnover_notional,
                    positions_json=excluded.positions_json,
                    position_notionals_json=excluded.position_notionals_json,
                    targets_json=excluded.targets_json,
                    scale=excluded.scale
                """,
                (
                    strategy_id,
                    spec_hash,
                    str(day)[:10],
                    float(nav),
                    float(daily_pnl),
                    float(long_pnl),
                    float(short_pnl),
                    float(funding_pnl),
                    float(cost_pnl),
                    int(bool(rebalance)),
                    json.dumps(changed_symbols, ensure_ascii=False),
                    json.dumps(sorted(set(resized_symbols)), ensure_ascii=False),
                    float(turnover_notional),
                    json.dumps(positions, ensure_ascii=False),
                    json.dumps(position_notionals, ensure_ascii=False, sort_keys=True),
                    json.dumps(targets, ensure_ascii=False, sort_keys=True),
                    float(scale),
                ),
            )
            self._conn.commit()

    def paper_nav(self, strategy_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if strategy_id:
                rows = self._conn.execute(
                    "SELECT * FROM paper_nav WHERE strategy_id=? ORDER BY day ASC",
                    (strategy_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM paper_nav ORDER BY strategy_id ASC, day ASC"
                ).fetchall()
        return [dict(row) for row in rows]

    def record_paper_rebalance(
        self,
        strategy_id: str,
        spec_hash: str,
        rebalance_date: str,
        signal_date: str,
        execution_date: str,
        targets: dict[str, int],
        changed_symbols: list[str],
        resized_symbols: list[str] | None = None,
        turnover_notional: float = 0.0,
        scale: float = 1.0,
    ) -> None:
        """记录 signal→execution→target-diff 事件；相同事件可重复写入。"""
        resized_symbols = resized_symbols or []
        with self._lock:
            targets_json = json.dumps(targets, ensure_ascii=False, sort_keys=True)
            changed_json = json.dumps(
                sorted(set(changed_symbols)), ensure_ascii=False
            )
            resized_json = json.dumps(
                sorted(set(resized_symbols)), ensure_ascii=False
            )
            existing = self._conn.execute(
                "SELECT * FROM paper_rebalances WHERE strategy_id=? AND rebalance_date=?",
                (strategy_id, str(rebalance_date)[:10]),
            ).fetchone()
            if existing is not None:
                if (
                    existing["spec_hash"] != spec_hash
                    or existing["targets_json"] != targets_json
                    or abs(float(existing["scale"] or 1.0) - float(scale)) > 1e-12
                ):
                    raise ValueError("同一 PAPER rebalance 日的 target、scale 或 spec 不一致")
                self._conn.execute(
                    """
                    UPDATE paper_rebalances
                       SET signal_date=?, execution_date=?, changed_json=?,
                           resized_json=?, turnover_notional=?, scale=?
                     WHERE strategy_id=? AND rebalance_date=?
                    """,
                    (
                        str(signal_date)[:10],
                        str(execution_date)[:10],
                        changed_json,
                        resized_json,
                        float(turnover_notional),
                        float(scale),
                        strategy_id,
                        str(rebalance_date)[:10],
                    ),
                )
                self._conn.commit()
                return
            self._conn.execute(
                """
                INSERT INTO paper_rebalances
                    (strategy_id, spec_hash, rebalance_date, signal_date,
                     execution_date, targets_json, changed_json,
                     resized_json, turnover_notional, scale)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    spec_hash,
                    str(rebalance_date)[:10],
                    str(signal_date)[:10],
                    str(execution_date)[:10],
                    targets_json,
                    changed_json,
                    resized_json,
                    float(turnover_notional),
                    float(scale),
                ),
            )
            self._conn.commit()

    def paper_rebalances(self, strategy_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if strategy_id:
                rows = self._conn.execute(
                    "SELECT * FROM paper_rebalances WHERE strategy_id=? ORDER BY rebalance_date ASC",
                    (strategy_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM paper_rebalances ORDER BY strategy_id ASC, rebalance_date ASC"
                ).fetchall()
        return [dict(row) for row in rows]

    def save_paper_portfolio(
        self,
        strategy_id: str,
        spec_hash: str,
        last_day: str | None,
        state: dict[str, Any],
        updated_at: datetime,
    ) -> None:
        with self._lock:
            existing = self._conn.execute(
                "SELECT spec_hash FROM paper_portfolios WHERE strategy_id=?",
                (strategy_id,),
            ).fetchone()
            if existing is not None and existing["spec_hash"] != spec_hash:
                raise ValueError("同一 PAPER strategy_id 不能混用不同 spec hash")
            self._conn.execute(
                """
                INSERT INTO paper_portfolios
                    (strategy_id, spec_hash, last_day, state_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    spec_hash=excluded.spec_hash,
                    last_day=excluded.last_day,
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (
                    strategy_id,
                    spec_hash,
                    str(last_day)[:10] if last_day is not None else None,
                    json.dumps(state, ensure_ascii=False, sort_keys=True),
                    _iso(updated_at),
                ),
            )
            self._conn.commit()

    def paper_portfolio(self, strategy_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM paper_portfolios WHERE strategy_id=?", (strategy_id,)
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["state"] = json.loads(out.pop("state_json"))
        return out
