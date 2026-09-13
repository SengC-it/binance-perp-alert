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
    signal_date   TEXT NOT NULL,
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
        self._conn.commit()

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
    ) -> int | None:
        """登记一条待验证信号。已存在同一 (策略, 信号日, 持有期) 时返回 None。"""
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO paper_trades
                        (strategy, signal_date, horizon_days, lookback_days,
                         k_long, k_short, longs_json, shorts_json, entry_prices)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        strategy,
                        signal_date,
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

    def paper_stats(self) -> dict[str, Any]:
        """滚动对账统计。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='verified' THEN 1 ELSE 0 END) AS verified,
                       SUM(CASE WHEN status='pending'  THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN status='failed'   THEN 1 ELSE 0 END) AS failed
                  FROM paper_trades
                """
            ).fetchone()
            rets = [
                float(r["net_return_pct"])
                for r in self._conn.execute(
                    "SELECT net_return_pct FROM paper_trades WHERE status='verified'"
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
