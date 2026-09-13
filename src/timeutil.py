"""时区与时间窗口工具。

Windows 不自带 IANA 时区数据库，zoneinfo 可能抛 ZoneInfoNotFoundError。
因此提供回退：Asia/Singapore 全年 UTC+8 且无夏令时，固定偏移与真实时区完全等价。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo

_FALLBACK_OFFSETS = {
    "Asia/Singapore": 8,
    "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Tokyo": 9,
    "UTC": 0,
}


def resolve_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        hours = _FALLBACK_OFFSETS.get(name, 0)
        return timezone(timedelta(hours=hours))


def in_quiet_hours(now_local: datetime, start: time, end: time) -> bool:
    """判断本地时间是否落在静默窗口内。支持跨午夜（如 23:30 -> 07:00）。"""
    current = now_local.time()
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def local_day_start(now: datetime, tz: tzinfo) -> datetime:
    """返回 now 所在本地日期的零点（带时区）。"""
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def fmt_local(dt: datetime | None, tz: tzinfo) -> str:
    if dt is None:
        return "-"
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
