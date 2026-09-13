"""投递层测试：分级路由、摘要路由、通道限流、失败降级。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import ChannelConfig, Config
from src.models import Alert, Severity
from src.notifier import Notifier
from src.store import Store

NOW = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
TZ = timezone.utc


class FakeApprise:
    """记录调用次数的假通道。"""

    def __init__(self, ok: bool = True, raises: bool = False):
        self.ok = ok
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, body: str) -> bool:
        self.calls.append((title, body))
        if self.raises:
            raise RuntimeError("模拟通道异常")
        return self.ok


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def make_notifier(store, specs, cfg: Config | None = None):
    """构造一个通道已被假对象替换的 Notifier。"""
    from collections import defaultdict

    cfg = cfg or Config(dry_run=False)
    n = Notifier.__new__(Notifier)
    n.cfg = cfg
    n.store = store
    n.tz = TZ
    n._sent_by_channel = defaultdict(list)
    n.channels = specs
    return n


def make_alert(severity=Severity.CRITICAL, rule_id="liq_distance_insufficient") -> Alert:
    return Alert(rule_id, "BTCUSDT:LONG", severity, "标题", "正文",
                 symbol="BTCUSDT", created_at=NOW)


# ---------------------------------------------------------------------------
# 分级路由
# ---------------------------------------------------------------------------

def test_critical_goes_to_both_channels(store):
    tg, mail = FakeApprise(), FakeApprise()
    specs = [
        (ChannelConfig("telegram", "tgram://x", ("CRITICAL", "WARN"), digest=False), tg),
        (ChannelConfig("email", "mailtos://x", ("CRITICAL", "WARN", "INFO"), digest=True), mail),
    ]
    n = make_notifier(store, specs)
    assert n.send("主题", "正文", severity=Severity.CRITICAL, now=NOW) is True
    assert len(tg.calls) == 1
    assert len(mail.calls) == 1


def test_info_only_goes_to_email(store):
    tg, mail = FakeApprise(), FakeApprise()
    specs = [
        (ChannelConfig("telegram", "tgram://x", ("CRITICAL", "WARN"), digest=False), tg),
        (ChannelConfig("email", "mailtos://x", ("CRITICAL", "WARN", "INFO"), digest=True), mail),
    ]
    n = make_notifier(store, specs)
    n.send("机会提醒", "正文", severity=Severity.INFO, now=NOW)
    assert tg.calls == [], "机会类 INFO 不应进 Telegram"
    assert len(mail.calls) == 1


def test_warn_goes_to_both(store):
    tg, mail = FakeApprise(), FakeApprise()
    specs = [
        (ChannelConfig("telegram", "tgram://x", ("CRITICAL", "WARN")), tg),
        (ChannelConfig("email", "mailtos://x", ("CRITICAL", "WARN", "INFO")), mail),
    ]
    n = make_notifier(store, specs)
    n.send("警告", "正文", severity=Severity.WARN, now=NOW)
    assert len(tg.calls) == 1
    assert len(mail.calls) == 1


def test_digest_skips_non_digest_channels(store):
    tg, mail = FakeApprise(), FakeApprise()
    specs = [
        (ChannelConfig("telegram", "tgram://x", digest=False), tg),
        (ChannelConfig("email", "mailtos://x", digest=True), mail),
    ]
    n = make_notifier(store, specs)
    n.send("摘要", "正文", severity=None, digest=True, now=NOW)
    assert tg.calls == []
    assert len(mail.calls) == 1


def test_severity_none_broadcasts_to_all(store):
    tg, mail = FakeApprise(), FakeApprise()
    specs = [
        (ChannelConfig("telegram", "tgram://x"), tg),
        (ChannelConfig("email", "mailtos://x"), mail),
    ]
    n = make_notifier(store, specs)
    n.send("自检", "正文", severity=None, now=NOW)
    assert len(tg.calls) == 1
    assert len(mail.calls) == 1


def test_channels_for_reports_routing(store):
    specs = [
        (ChannelConfig("telegram", "tgram://x", ("CRITICAL",)), FakeApprise()),
        (ChannelConfig("email", "mailtos://x", ("CRITICAL", "WARN", "INFO")), FakeApprise()),
    ]
    n = make_notifier(store, specs)
    assert n.channels_for(Severity.CRITICAL) == ["telegram", "email"]
    assert n.channels_for(Severity.INFO) == ["email"]


# ---------------------------------------------------------------------------
# 失败降级
# ---------------------------------------------------------------------------

def test_one_channel_failure_does_not_block_the_other(store):
    tg, mail = FakeApprise(ok=False), FakeApprise(ok=True)
    specs = [
        (ChannelConfig("telegram", "tgram://x"), tg),
        (ChannelConfig("email", "mailtos://x"), mail),
    ]
    n = make_notifier(store, specs)
    assert n.send("主题", "正文", severity=Severity.CRITICAL, now=NOW) is True
    assert len(mail.calls) == 1


def test_channel_exception_is_isolated(store):
    tg, mail = FakeApprise(raises=True), FakeApprise()
    specs = [
        (ChannelConfig("telegram", "tgram://x"), tg),
        (ChannelConfig("email", "mailtos://x"), mail),
    ]
    n = make_notifier(store, specs)
    assert n.send("主题", "正文", severity=Severity.CRITICAL, now=NOW) is True
    assert len(mail.calls) == 1, "Telegram 抛异常不应影响邮件"


def test_all_channels_failing_returns_false(store):
    specs = [
        (ChannelConfig("a", "x"), FakeApprise(ok=False)),
        (ChannelConfig("b", "y"), FakeApprise(ok=False)),
    ]
    n = make_notifier(store, specs)
    assert n.send("主题", "正文", now=NOW) is False


def test_no_channels_returns_false(store):
    n = make_notifier(store, [])
    assert n.send("主题", "正文", now=NOW) is False


# ---------------------------------------------------------------------------
# 通道级限流
# ---------------------------------------------------------------------------

def test_channel_hourly_limit_is_enforced(store):
    tg = FakeApprise()
    specs = [(ChannelConfig("telegram", "tgram://x", max_per_hour=2), tg)]
    n = make_notifier(store, specs)
    for _ in range(5):
        n.send("主题", "正文", severity=Severity.CRITICAL, now=NOW)
    assert len(tg.calls) == 2, "通道级上限应只放行 2 条"


def test_channel_limit_window_slides(store):
    tg = FakeApprise()
    specs = [(ChannelConfig("telegram", "tgram://x", max_per_hour=1), tg)]
    n = make_notifier(store, specs)
    n.send("主题", "正文", now=NOW)
    n.send("主题", "正文", now=NOW)
    assert len(tg.calls) == 1
    later = NOW + timedelta(hours=2)
    n.send("主题", "正文", now=later)
    assert len(tg.calls) == 2, "超出 1 小时窗口后应重新放行"


def test_global_limit_blocks_immediate_dispatch(store):
    tg = FakeApprise()
    specs = [(ChannelConfig("telegram", "tgram://x"), tg)]
    cfg = Config(dry_run=False, max_per_hour=1)
    n = make_notifier(store, specs, cfg)
    # 先占用全局额度
    store.insert_alert(make_alert(), sent_at=NOW)
    n.dispatch(make_alert(), immediate=True, now=NOW)
    assert tg.calls == [], "全局额度用尽时不应立即发送"


# ---------------------------------------------------------------------------
# dispatch 行为
# ---------------------------------------------------------------------------

def test_dispatch_success_marks_sent(store):
    tg = FakeApprise()
    specs = [(ChannelConfig("telegram", "tgram://x"), tg)]
    n = make_notifier(store, specs)
    n.dispatch(make_alert(), immediate=True, now=NOW)
    assert len(tg.calls) == 1
    assert store.pending_alerts() == [], "成功投递后不应留在待发队列"


def test_dispatch_failure_queues_for_digest(store):
    tg = FakeApprise(ok=False)
    specs = [(ChannelConfig("telegram", "tgram://x"), tg)]
    n = make_notifier(store, specs)
    n.dispatch(make_alert(), immediate=True, now=NOW)
    assert len(store.pending_alerts()) == 1, "投递失败应转入摘要队列，避免静默丢失"


def test_dispatch_non_immediate_queues(store):
    tg = FakeApprise()
    specs = [(ChannelConfig("telegram", "tgram://x"), tg)]
    n = make_notifier(store, specs)
    n.dispatch(make_alert(severity=Severity.INFO), immediate=False, now=NOW)
    assert tg.calls == []
    assert len(store.pending_alerts()) == 1


def test_send_digest_marks_all_sent(store):
    mail = FakeApprise()
    specs = [(ChannelConfig("email", "mailtos://x", digest=True), mail)]
    n = make_notifier(store, specs)
    for i in range(3):
        store.insert_alert(make_alert(severity=Severity.WARN), sent_at=None)
    assert n.send_digest(NOW) is True
    assert store.pending_alerts() == []


def test_render_digest_returns_none_when_empty(store):
    n = make_notifier(store, [(ChannelConfig("email", "mailtos://x"), FakeApprise())])
    assert n.render_digest(NOW) is None
