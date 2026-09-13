"""多通道投递层（SMTP 邮件 + Telegram）。

基于 Apprise 统一封装通道。Apprise 负责协议细节，本模块负责它不管的三件事：

1. **分级路由**：风险类告警（CRITICAL/WARN）可以同时走 Telegram 与邮件；
   机会类信息（INFO）只进邮件摘要——机会信息不该半夜把人叫醒。
2. **全局限流**：每小时最多发多少封，避免告警风暴。
3. **每日摘要聚合**：把碎片化 INFO/WARN 合成一封。

通道配置见 config.yaml 的 `notify.channels`，URL 从环境变量读取，不落盘。

支持的 URL 形式（Apprise 语法）：
  SMTP 邮件  mailtos://用户:应用专用密码@gmail.com?to=收件人
  Telegram   tgram://<bot_token>/<chat_id>

本层是整条链路的终点：消息发出即结束，不存在任何「收到回执后触发下单」的回流路径。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, tzinfo
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .config import ChannelConfig, Config
from .models import Alert, Severity
from .scope_guard import SCOPE_DISCLAIMER
from .store import Store
from .timeutil import fmt_local

log = logging.getLogger(__name__)

DIGEST_COMPONENT = "digest"


def with_from_name(url: str, name: str) -> str:
    """给 SMTP 类 URL 补上发件人名称参数。

    Apprise 的 mailto 插件默认把发件人名写成 "Apprise"，config.yaml 的
    email.from_name 只有显式作为 URL 的 name 参数传入才生效——
    否则那项配置形同虚设，收件人看到的是 "Apprise" 而不是设定的名字。

    safe 里保留 : / @ , 是为了不破坏收件人邮箱（to=a@x.com,b@y.com）。
    """
    if not name or not url.lower().startswith(("mailto:", "mailtos:")):
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["name"] = name
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            # quote_via=quote：空格编码为 %20 而非 +。Apprise 不解码 +，
            # 用默认的 quote_plus 会让发件人名显示成 "Work+Alert"。
            urlencode(query, safe=":/@,", quote_via=quote),
            parts.fragment,
        )
    )


class Notifier:
    def __init__(self, cfg: Config, store: Store, tz: tzinfo):
        self.cfg = cfg
        self.store = store
        self.tz = tz
        self.channels: list[tuple[ChannelConfig, Any]] = []
        # 通道级限流计数（进程内）。全局限流由 store 承担，
        # 这里只是防止某个通道被单独打爆；进程重启会清零，
        # 但全局上限仍然生效，不会造成无限发送。
        self._sent_by_channel: dict[str, list[datetime]] = defaultdict(list)

        if not cfg.dry_run:
            self.channels = self._build_channels()

    # ---------- 通道构建 ----------

    def _build_channels(self) -> list[tuple[ChannelConfig, Any]]:
        import apprise

        specs: list[ChannelConfig] = list(self.cfg.notify_channels)
        if not specs and self.cfg.apprise_url:
            specs = [ChannelConfig(name="default", url=self.cfg.apprise_url)]
        if not specs:
            raise RuntimeError(
                "没有可用的投递通道。请设置 APPRISE_URL，"
                "或在 config.yaml 的 notify.channels 中至少配置一个。"
            )

        built: list[tuple[ChannelConfig, Any]] = []
        for spec in specs:
            obj = apprise.Apprise()
            if not obj.add(with_from_name(spec.url, self.cfg.email_from_name)):
                # 单个通道解析失败不应拖垮其他通道，但要让人看得见。
                log.error("通道 %s 的 URL 无法解析，已跳过（检查环境变量是否填对）", spec.name)
                continue
            built.append((spec, obj))
            log.info("投递通道已就绪：%s（分级 %s，摘要 %s）",
                     spec.name, "/".join(spec.severities),
                     "是" if spec.digest else "否")

        if not built:
            raise RuntimeError("所有投递通道都无法解析，请检查 URL 格式")
        return built

    # ---------- 限流 ----------

    def rate_limited(self, now: datetime) -> bool:
        """全局限流：所有通道合计每小时上限。"""
        return self.store.sent_count_since(now - timedelta(hours=1)) >= self.cfg.max_per_hour

    def _channel_limited(self, spec: ChannelConfig, now: datetime) -> bool:
        if spec.max_per_hour is None:
            return False
        window = now - timedelta(hours=1)
        recent = [t for t in self._sent_by_channel[spec.name] if t >= window]
        self._sent_by_channel[spec.name] = recent
        return len(recent) >= spec.max_per_hour

    def _mark_sent(self, spec: ChannelConfig, now: datetime) -> None:
        self._sent_by_channel[spec.name].append(now)

    # ---------- 底层发送 ----------

    def send(
        self,
        subject: str,
        body: str,
        severity: Severity | None = None,
        digest: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """按分级路由发送。返回是否至少有一个通道成功。

        severity=None 表示发给全部通道（用于自检、摘要）。
        digest=True 表示只发给「接收摘要」的通道。
        """
        if self.cfg.dry_run:
            log.info("[DRY-RUN] 未发送 | 主题：%s", subject)
            log.debug("[DRY-RUN] 正文：\n%s", body)
            return True

        if not self.channels:
            log.error("没有可用通道，消息被丢弃：%s", subject)
            return False

        stamp = now or datetime.now(self.tz)
        sent_any = False
        for spec, obj in self.channels:
            if severity is not None and not spec.accepts(severity.value):
                continue
            if digest and not spec.digest:
                continue
            if self._channel_limited(spec, stamp):
                log.warning("通道 %s 达到每小时上限，本次跳过", spec.name)
                continue
            try:
                ok = bool(obj.notify(title=subject, body=body))
            except Exception as exc:  # 单通道异常不应影响其他通道
                log.error("通道 %s 发送异常：%s", spec.name, exc)
                continue
            if ok:
                sent_any = True
                self._mark_sent(spec, stamp)
            else:
                log.error("通道 %s 发送失败：%s", spec.name, subject)

        return sent_any

    def channels_for(self, severity: Severity) -> list[str]:
        """该分级会走哪些通道（供自检与测试用）。"""
        return [spec.name for spec, _ in self.channels if spec.accepts(severity.value)]

    # ---------- 告警投递 ----------

    def dispatch(self, alert: Alert, immediate: bool, now: datetime) -> None:
        """立即发或进摘要队列。两种情况下都会写入 alerts 表以便审计。"""
        if immediate and not self.rate_limited(now):
            subject, body = alert.to_email()
            ok = self.send(
                subject,
                self._wrap(body, alert),
                severity=alert.severity,
                now=now,
            )
            # 发送失败则留作待发，随下一次摘要重试，避免告警静默丢失
            self.store.insert_alert(alert, sent_at=now if ok else None)
            if not ok:
                log.warning("立即投递失败，已转入摘要队列：%s", alert.title)
        else:
            self.store.insert_alert(alert, sent_at=None)
            log.info("告警进入每日摘要队列：%s", alert.title)

    @staticmethod
    def _wrap(body: str, alert: Alert) -> str:
        return (
            f"{body}\n\n"
            f"----\n"
            f"规则：{alert.rule_id}\n"
            f"去重键：{alert.dedup_key}\n"
            f"分级：{alert.severity.value}\n"
            f"本消息由 Work Alert 自动发送，请勿直接回复。\n\n"
            f"{SCOPE_DISCLAIMER}"
        )

    # ---------- 每日摘要 ----------

    def pending(self) -> list[Alert]:
        return self.store.pending_alerts()

    def render_digest(self, now: datetime) -> tuple[str, str, list[Alert]] | None:
        """生成摘要的主题与正文。无待发告警时返回 None。"""
        items = self.pending()
        if not items:
            return None

        grouped: dict[Severity, list[Alert]] = {}
        for item in items:
            grouped.setdefault(item.severity, []).append(item)

        lines: list[str] = [
            f"统计区间：至 {fmt_local(now, self.tz)}",
            f"待发告警共 {len(items)} 条",
            "",
        ]
        for sev in (Severity.CRITICAL, Severity.WARN, Severity.INFO):
            group = grouped.get(sev)
            if not group:
                continue
            lines.append(f"【{sev.value}】{len(group)} 条")
            for item in group:
                ts = fmt_local(item.created_at, self.tz)
                lines.append(f"  · [{ts}] {item.title}")
                first_line = item.body.strip().splitlines()[0] if item.body.strip() else ""
                if first_line:
                    lines.append(f"      {first_line}")
            lines.append("")

        lines.append("说明：CRITICAL 级告警已在触发时单独发送，此处汇总的是其余告警。")
        lines.append("")
        lines.append(SCOPE_DISCLAIMER)
        subject = f"[摘要] {len(items)} 条告警 | {fmt_local(now, self.tz)}"
        return subject, "\n".join(lines), items

    def send_digest(self, now: datetime) -> bool:
        rendered = self.render_digest(now)
        if rendered is None:
            log.info("每日摘要：无待发告警，跳过")
            return False
        subject, body, items = rendered
        ok = self.send(subject, body, severity=None, digest=True, now=now)
        if ok:
            for item in items:
                if item.alert_id is not None:
                    self.store.mark_sent(item.alert_id, now)
        return ok

    def should_send_digest(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        if local.time() < self.cfg.digest_at:
            return False
        last = self.store.heartbeat_last_ok(DIGEST_COMPONENT)
        if last is not None and last.astimezone(self.tz).date() == local.date():
            return False
        return True

    def mark_digest_sent(self, now: datetime) -> None:
        self.store.heartbeat_ok(DIGEST_COMPONENT, now, "digest sent")

    # ---------- 自检 ----------

    def send_test(self, now: datetime) -> bool:
        names = ", ".join(spec.name for spec, _ in self.channels) or "（无）"
        return self.send(
            "[测试] Work Alert 投递通道自检",
            "这是一条通道自检消息。\n\n"
            f"已配置通道：{names}\n"
            f"发送时间：{fmt_local(now, self.tz)}\n\n"
            "如果你在 Telegram 和邮箱都收到了它，说明两条链路都正常。\n",
            severity=None,
            now=now,
        )
