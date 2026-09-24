"""交易日历与调度判断：一律用交易所日历 + zoneinfo，不写死 UTC 偏移。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import exchange_calendars as xcals
import pandas as pd

from .common import load_yaml, utcnow


def _override(cal_code: str) -> set[date]:
    extra = load_yaml("holidays_override.yaml").get(cal_code) or []
    return {pd.Timestamp(d).date() for d in extra}


def calendar(cal_code: str):
    cal = xcals.get_calendar(cal_code)
    # 防御：日历库未覆盖当年（假日数据缺失）时直接报错，而不是默认开市
    if cal.last_session.date() < date.today():
        raise RuntimeError(f"{cal_code} 日历只覆盖到 {cal.last_session.date()}，请升级 exchange_calendars")
    return cal


def is_session(cal_code: str, d: date) -> bool:
    if d in _override(cal_code):
        return False
    return calendar(cal_code).is_session(pd.Timestamp(d))


def session_close(cal_code: str, d: date) -> datetime:
    """该交易日收盘时刻（UTC，已考虑提前收盘）。"""
    return calendar(cal_code).session_close(pd.Timestamp(d)).to_pydatetime()


def latest_session(cal_code: str, now: datetime | None = None) -> date | None:
    """now 时刻之前已经收盘的最近一个交易日。"""
    now = now or utcnow()
    cal = calendar(cal_code)
    d = now.date() + timedelta(days=1)
    for _ in range(15):
        if is_session(cal_code, d) and cal.session_close(pd.Timestamp(d)).to_pydatetime() <= now:
            return d
        d -= timedelta(days=1)
    return None


def due_session(cal_code: str, delay_minutes: int, now: datetime | None = None) -> date | None:
    """若最近一个交易日已收盘且超过延迟时间，返回该交易日，否则 None。"""
    now = now or utcnow()
    d = latest_session(cal_code, now)
    if d is None:
        return None
    if now < session_close(cal_code, d) + timedelta(minutes=delay_minutes):
        return None
    return d


def latest_session_on_or_before(cal_code: str, d: date) -> date:
    """d 当天或之前最近的交易日（不看是否已收盘，用于跨市场对齐日期）。"""
    for _ in range(20):
        if is_session(cal_code, d):
            return d
        d -= timedelta(days=1)
    raise RuntimeError(f"{cal_code}: 20 天内无交易日")
