"""当日收盘价补齐：Yahoo 日线缺当日收盘（伦敦收盘竞价延迟、周末回溯丢失等）时，用第二源报价补齐。

只有第二源的「前收」与 Yahoo 前一交易日收盘一致（±2%）时才采用，防止代码映射到其他证券（如 AV-GB ≠ Aviva）。
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd


def patch_close(df: pd.DataFrame | None, q: dict | None, session: date, tz: str) -> tuple[pd.DataFrame | None, str | None]:
    """若 df 缺当日收盘，用 CNBC 报价（且报价时间为当日）补齐。返回 (df, 收盘来源)。"""
    if df is None or not len(df):
        return df, None
    last_d = df.index[-1].date()
    if last_d == session and df["Close"].iloc[-1] == df["Close"].iloc[-1]:
        return df.dropna(subset=["Close"]), "Yahoo Finance"
    qt = datetime.fromisoformat(q["time"]).astimezone(ZoneInfo(tz)).date() if q and q.get("time") else None
    if not q or qt != session:
        return df.dropna(subset=["Close"]), None
    # 代码映射校验：CNBC 的前收须与 Yahoo 前一交易日收盘一致，否则视为不同证券（如 AV-GB ≠ Aviva）
    prev = df.dropna(subset=["Close"])
    prev = prev[prev.index.date < session]["Close"]
    if not len(prev) or not q.get("prev_close") or abs(q["prev_close"] / prev.iloc[-1] - 1) > 0.02:
        return df.dropna(subset=["Close"]), "MISMATCH"
    df = df.copy()
    if last_d == session:
        df.iloc[-1, df.columns.get_loc("Close")] = q["last"]
    else:
        row = pd.DataFrame({"Open": [None], "High": [None], "Low": [None], "Close": [q["last"]], "Volume": [None]},
                           index=pd.DatetimeIndex([pd.Timestamp(session, tz=df.index.tz)]))
        df = pd.concat([df, row])
    return df.dropna(subset=["Close"]), ("CNBC（Yahoo 日线缺当日收盘）" if q.get("source") == "CNBC quote" else "Yahoo 报价（Yahoo 日线缺当日收盘）")

