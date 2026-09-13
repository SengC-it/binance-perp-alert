"""引擎调度测试：持仓轮询开关（纯信号模式）。

positions_enabled=false 时守护循环完全不读账户，
因此只想要信号提醒的用户可以不配置任何 API Key。
"""

from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import load_config
from src.engine import AlertService

# 拉到极大，确保一轮循环内只有持仓轮询可能被触发。
NEVER = 10**9


class _StopLoop(Exception):
    """用于中断 run_forever 的无限循环。"""


def _write_config(tmp_path: Path, enabled: bool) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
timezone: UTC
db_path: {tmp_path / "t.db"}
poll:
  positions_enabled: {"true" if enabled else "false"}
  positions_seconds: 0
  opportunity_seconds: {NEVER}
  cross_seconds: {NEVER}
  directional_seconds: {NEVER}
  heartbeat_seconds: {NEVER}
  heartbeat_stale_seconds: {NEVER}
email:
  digest_at: "23:59"
cross_exchange:
  enabled: false
""",
        encoding="utf-8",
    )
    return path


def _make_store() -> MagicMock:
    """AlertService 构造时会写基线心跳，store 必须可用。"""
    store = MagicMock()
    store.heartbeat_last_ok.return_value = None
    store.pending_alerts.return_value = []
    store.sent_count_since.return_value = 0
    return store


def _run_one_loop(tmp_path: Path, enabled: bool, monkeypatch) -> list[str]:
    cfg = load_config(
        _write_config(tmp_path, enabled),
        tmp_path / ".env",
        dry_run=True,
        require_credentials=False,
    )
    service = AlertService(
        cfg,
        timezone.utc,
        _make_store(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    calls: list[str] = []
    service.poll_positions = lambda now: calls.append("positions")

    monkeypatch.setattr(
        "src.engine.time.sleep", lambda s: (_ for _ in ()).throw(_StopLoop())
    )
    with pytest.raises(_StopLoop):
        service.run_forever()
    return calls


def test_positions_polled_when_enabled(tmp_path, monkeypatch):
    assert _run_one_loop(tmp_path, True, monkeypatch) == ["positions"]


def test_positions_skipped_when_disabled(tmp_path, monkeypatch):
    """关闭后一轮循环内不应有任何账户读取。"""
    assert _run_one_loop(tmp_path, False, monkeypatch) == []


def test_positions_enabled_defaults_to_true(tmp_path):
    """未显式配置时应保持向后兼容：默认开启持仓轮询。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
timezone: UTC
db_path: {tmp_path / "t.db"}
cross_exchange:
  enabled: false
""",
        encoding="utf-8",
    )
    cfg = load_config(path, tmp_path / ".env", dry_run=True, require_credentials=False)
    assert cfg.positions_enabled is True
