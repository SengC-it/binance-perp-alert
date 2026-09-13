"""配置加载与校验。

配置分两部分：
  - config.yaml  非敏感配置（阈值、规则开关、静默期等）
  - .env         敏感配置（币安只读 API Key、Apprise 投递地址）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import yaml

VALID_SEVERITIES = {"CRITICAL", "WARN", "INFO"}


class ConfigError(Exception):
    """配置缺失或非法。"""


@dataclass
class RuleConfig:
    """单条规则的开关与覆盖项。"""

    rule_id: str
    enabled: bool = True
    severity: str | None = None
    confirmations: int | None = None
    cooldown_seconds: int | None = None


@dataclass
class ChannelConfig:
    """一个投递通道。

    通道之间按分级路由：风险类告警可以同时走 Telegram 与邮件，
    而机会类信息只进邮件摘要——机会信息不该半夜把人叫醒。
    """

    name: str
    url: str
    severities: tuple[str, ...] = ("CRITICAL", "WARN", "INFO")
    digest: bool = True
    max_per_hour: int | None = None      # None 表示沿用全局上限

    def accepts(self, severity: str) -> bool:
        return severity in self.severities

    def __post_init__(self) -> None:
        self.severities = tuple(s.upper() for s in self.severities)


@dataclass
class Config:
    # app
    timezone: str = "Asia/Singapore"
    db_path: Path = Path("./data/alerts.db")
    log_level: str = "INFO"

    # binance
    binance_base_url: str = "https://fapi.binance.com"
    recv_window: int = 5000
    timeout_seconds: int = 15

    # poll
    # 关闭后完全不读取账户，守护循环只跑公开行情类扫描。
    # 只想要信号提醒、不想配置 API Key 时设为 false。
    positions_enabled: bool = True
    positions_seconds: int = 30
    heartbeat_seconds: int = 60
    heartbeat_stale_seconds: int = 180
    api_error_threshold: int = 5
    opportunity_seconds: int = 300
    cross_seconds: int = 600
    directional_seconds: int = 3600   # 日线信号，每小时检查一次足够

    # email
    email_from_name: str = "Work Alert"
    max_per_hour: int = 12
    quiet_start: time = time(23, 30)
    quiet_end: time = time(7, 0)
    digest_at: time = time(8, 0)

    # thresholds
    thresholds: dict[str, float] = field(default_factory=dict)

    # 跨所扫描
    cross_enabled: bool = True
    cross_exchanges: list[str] = field(default_factory=lambda: ["bybit", "okx"])
    bybit_base_url: str = "https://api.bybit.com"
    okx_base_url: str = "https://www.okx.com"
    okx_max_symbols: int = 60

    # 标的 -> 计划止损距离（%）
    symbols: dict[str, float] = field(default_factory=dict)

    # 规则配置
    rules: dict[str, RuleConfig] = field(default_factory=dict)

    cooldown_default: int = 14400
    cooldown_critical: int = 1800

    # 敏感项
    api_key: str = ""
    api_secret: str = ""
    apprise_url: str = ""

    # 多通道投递（为空时回退到 apprise_url 单一通道）
    notify_channels: list[ChannelConfig] = field(default_factory=list)

    # 运行模式
    dry_run: bool = False

    def planned_stop_pct(self, symbol: str) -> float | None:
        """返回该标的的计划止损距离（小数，如 0.02）。未配置返回 None。"""
        pct = self.symbols.get(symbol.upper())
        if pct is None or pct <= 0:
            return None
        return pct / 100.0

    def rule(self, rule_id: str) -> RuleConfig:
        return self.rules.get(rule_id, RuleConfig(rule_id=rule_id))

    def validate(self, require_credentials: bool = True) -> None:
        """校验必填项。密钥缺失直接报错，避免带病启动。

        require_credentials=False 用于只用公开接口的场景（如机会扫描），
        此时不需要 API Key 与投递地址。
        """
        problems: list[str] = []
        if require_credentials:
            # 纯信号模式（positions_enabled=false）完全不读账户，
            # 因此不该要求账户凭证——否则用户会被迫为一个用不到的能力配密钥。
            if self.positions_enabled:
                if not self.api_key:
                    problems.append(
                        "缺少 BINANCE_API_KEY（请检查 .env；"
                        "若只做信号提醒，可在 config.yaml 设 poll.positions_enabled: false）"
                    )
                if not self.api_secret:
                    problems.append(
                        "缺少 BINANCE_API_SECRET（请检查 .env；"
                        "若只做信号提醒，可在 config.yaml 设 poll.positions_enabled: false）"
                    )
            if not self.apprise_url and not self.notify_channels and not self.dry_run:
                problems.append(
                    "没有可用的投递通道：请设置 APPRISE_URL，"
                    "或在 config.yaml 的 notify.channels 中至少配置一个（并填好对应的环境变量）"
                )
        if self.max_per_hour <= 0:
            problems.append("email.max_per_hour 必须为正整数")
        for rule_id, rc in self.rules.items():
            if rc.severity is not None and rc.severity not in VALID_SEVERITIES:
                problems.append(f"规则 {rule_id} 的 severity 非法：{rc.severity}")
            if rc.confirmations is not None and rc.confirmations < 1:
                problems.append(f"规则 {rule_id} 的 confirmations 必须 >= 1")
        if problems:
            raise ConfigError("配置校验失败：\n  - " + "\n  - ".join(problems))


def load_env_file(path: str | Path) -> dict[str, str]:
    """极简 .env 解析，避免引入额外依赖。已存在的环境变量优先。"""
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def _parse_hhmm(value: Any, default: time) -> time:
    if value is None:
        return default
    if isinstance(value, time):
        return value
    text = str(value).strip()
    try:
        hh, mm = text.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"时间格式应为 HH:MM，收到：{value!r}") from exc


def _as_int(value: Any, default: int) -> int:
    return default if value is None else int(value)


def _as_float(value: Any, default: float) -> float:
    return default if value is None else float(value)


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path = ".env",
    dry_run: bool = False,
    require_credentials: bool = True,
) -> Config:
    """加载并校验配置。env 值覆盖 yaml 中的非敏感默认项。"""
    path = Path(config_path)
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ConfigError("config.yaml 顶层必须是映射（mapping）")
            raw = loaded

    env = load_env_file(env_path)
    for key, value in env.items():
        os.environ.setdefault(key, value)

    app = raw.get("app") or {}
    bnc = raw.get("binance") or {}
    poll = raw.get("poll") or {}
    email = raw.get("email") or {}
    quiet = email.get("quiet_hours") or {}

    symbols: dict[str, float] = {}
    for sym, spec in (raw.get("symbols") or {}).items():
        if isinstance(spec, dict):
            pct = spec.get("planned_stop_pct")
        else:
            pct = spec
        if pct is None:
            continue
        symbols[str(sym).upper()] = float(pct)

    rules: dict[str, RuleConfig] = {}
    for rule_id, spec in (raw.get("rules") or {}).items():
        spec = spec or {}
        rules[rule_id] = RuleConfig(
            rule_id=rule_id,
            enabled=bool(spec.get("enabled", True)),
            severity=spec.get("severity"),
            confirmations=spec.get("confirmations"),
            cooldown_seconds=spec.get("cooldown_seconds"),
        )

    cooldown = raw.get("cooldown") or {}
    cross = raw.get("cross_exchange") or {}
    exchanges = cross.get("exchanges") or ["bybit", "okx"]
    if isinstance(exchanges, str):
        exchanges = [item.strip() for item in exchanges.split(",") if item.strip()]

    cfg = Config(
        timezone=str(app.get("timezone", "Asia/Singapore")),
        db_path=Path(app.get("db_path", "./data/alerts.db")),
        log_level=str(app.get("log_level", "INFO")).upper(),
        binance_base_url=str(bnc.get("base_url", "https://fapi.binance.com")).rstrip("/"),
        recv_window=_as_int(bnc.get("recv_window"), 5000),
        timeout_seconds=_as_int(bnc.get("timeout_seconds"), 15),
        positions_enabled=bool(poll.get("positions_enabled", True)),
        positions_seconds=_as_int(poll.get("positions_seconds"), 30),
        heartbeat_seconds=_as_int(poll.get("heartbeat_seconds"), 60),
        heartbeat_stale_seconds=_as_int(poll.get("heartbeat_stale_seconds"), 180),
        api_error_threshold=_as_int(poll.get("api_error_threshold"), 5),
        opportunity_seconds=_as_int(poll.get("opportunity_seconds"), 300),
        cross_seconds=_as_int(poll.get("cross_seconds"), 600),
        directional_seconds=_as_int(poll.get("directional_seconds"), 3600),
        email_from_name=str(email.get("from_name", "Work Alert")),
        max_per_hour=_as_int(email.get("max_per_hour"), 12),
        quiet_start=_parse_hhmm(quiet.get("start"), time(23, 30)),
        quiet_end=_parse_hhmm(quiet.get("end"), time(7, 0)),
        digest_at=_parse_hhmm(email.get("digest_at"), time(8, 0)),
        thresholds={k: float(v) for k, v in (raw.get("thresholds") or {}).items()},
        cross_enabled=bool(cross.get("enabled", True)),
        cross_exchanges=[str(e).lower() for e in exchanges],
        bybit_base_url=str(cross.get("bybit_base_url", "https://api.bybit.com")),
        okx_base_url=str(cross.get("okx_base_url", "https://www.okx.com")),
        okx_max_symbols=_as_int(cross.get("okx_max_symbols"), 60),
        symbols=symbols,
        rules=rules,
        cooldown_default=_as_int(cooldown.get("default_seconds"), 14400),
        cooldown_critical=_as_int(cooldown.get("critical_seconds"), 1800),
        api_key=os.environ.get("BINANCE_API_KEY", "").strip(),
        api_secret=os.environ.get("BINANCE_API_SECRET", "").strip(),
        apprise_url=os.environ.get("APPRISE_URL", "").strip(),
        notify_channels=_parse_channels(raw.get("notify") or {}),
        dry_run=dry_run,
    )

    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}（请从 config.example.yaml 复制）")

    cfg.validate(require_credentials=require_credentials)
    return cfg


def _parse_channels(notify: dict[str, Any]) -> list[ChannelConfig]:
    """解析 notify.channels。

    每个通道的 URL 从环境变量读取（用 url_env 指定变量名），
    这样密钥不会落到 config.yaml 里。

    若未配置任何通道但设置了 APPRISE_URL，则回退为单一通道，
    保持与旧配置的兼容。
    """
    raw_channels = notify.get("channels") or []
    channels: list[ChannelConfig] = []

    for i, item in enumerate(raw_channels):
        if not isinstance(item, dict):
            raise ConfigError(f"notify.channels[{i}] 必须是映射")
        if item.get("enabled", True) is False:
            continue
        name = str(item.get("name") or f"channel{i}").strip()
        url = str(item.get("url") or "").strip()
        env_name = str(item.get("url_env") or "").strip()
        if not url and env_name:
            url = os.environ.get(env_name, "").strip()
        if not url:
            # 通道未配置 URL 时静默跳过，而不是让整个程序起不来。
            # 例如只填了 Telegram 却没填邮件密码，不该阻塞 Telegram 告警。
            continue

        severities = item.get("severities")
        if severities is None:
            sev_tuple = ("CRITICAL", "WARN", "INFO")
        else:
            if isinstance(severities, str):
                severities = [severities]
            sev_tuple = tuple(str(s).upper() for s in severities)
            unknown = [s for s in sev_tuple if s not in VALID_SEVERITIES]
            if unknown:
                raise ConfigError(
                    f"notify.channels[{i}].severities 含非法分级：{unknown}"
                )

        channels.append(
            ChannelConfig(
                name=name,
                url=url,
                severities=sev_tuple,
                digest=bool(item.get("digest", True)),
                max_per_hour=(
                    _as_int(item["max_per_hour"], 0)
                    if item.get("max_per_hour") is not None
                    else None
                ),
            )
        )

    if not channels:
        fallback = os.environ.get("APPRISE_URL", "").strip()
        if fallback:
            channels.append(ChannelConfig(name="default", url=fallback))

    return channels


def threshold(cfg: Config, key: str, default: float) -> float:
    return float(cfg.thresholds.get(key, default))
