"""时间工具：统一使用不带时区的 ISO 8601，按整分钟取桶。"""
from __future__ import annotations

from datetime import datetime, timedelta

FMT_MINUTE = "%Y-%m-%dT%H:%M"
FMT_SECOND = "%Y-%m-%dT%H:%M:%S"


def parse_ts(value: str) -> datetime:
    """解析日志时间。支持到分钟或到秒（可带小数秒），禁止带时区。"""
    if not isinstance(value, str):
        raise ValueError("时间必须是字符串")
    text = value.strip()
    if text.endswith("Z") or any(c in text for c in ("+",)):
        # 允许末尾 Z 与显式偏移的唯一方式是拒绝：本系统统一用本地无时区时间
        raise ValueError("时间不允许带时区，请使用 YYYY-MM-DDTHH:MM:SS")
    if "." in text:
        head, _, frac = text.partition(".")
        if not frac.isdigit():
            raise ValueError("小数秒格式错误")
        dt = datetime.strptime(head, FMT_SECOND).replace(microsecond=int(frac.ljust(6, "0")[:6]))
    elif len(text) == 16:
        dt = datetime.strptime(text, FMT_MINUTE)
    else:
        dt = datetime.strptime(text, FMT_SECOND)
    return dt


def minute_floor(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def fmt_minute(dt: datetime) -> str:
    return dt.strftime(FMT_MINUTE)


def fmt_second(dt: datetime) -> str:
    return dt.strftime(FMT_SECOND)


def minute_key(dt: datetime) -> str:
    return fmt_minute(minute_floor(dt))


def to_minute(dt: datetime) -> int:
    """整分钟键：日历序日 * 1440 + 当天分钟数。连续整数、可比较、可做 range，
    且不经过任何时区转换（所有时间均为无时区本地时间）。"""
    d = minute_floor(dt)
    return d.date().toordinal() * 1440 + d.hour * 60 + d.minute


def from_minute(minute: int) -> datetime:
    from datetime import date, time
    day, rem = divmod(minute, 1440)
    return datetime.combine(date.fromordinal(day), time(rem // 60, rem % 60))


def minutes_between(start: datetime, end_exclusive: datetime) -> int:
    """左闭右开的整分钟区间长度（分钟）。"""
    return to_minute(end_exclusive) - to_minute(start)


def plus_minutes(dt: datetime, n: int) -> datetime:
    return dt + timedelta(minutes=n)
