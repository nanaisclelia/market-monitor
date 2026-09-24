"""yfinance：日线历史（主数据源）。分批下载 + 限流重试。"""
from __future__ import annotations

import time

import pandas as pd
import yfinance as yf

from ..common import CACHE, get_logger

CACHE.mkdir(parents=True, exist_ok=True)
yf.set_tz_cache_location(str(CACHE / "yf_tz"))
log = get_logger()


def history(tickers: list[str], period: str = "1y", chunk: int = 100,
            retries: int = 3) -> dict[str, pd.DataFrame]:
    """返回 {ticker: DataFrame[Open, High, Low, Close, Volume]}，下载失败的 ticker 不在结果中。"""
    out: dict[str, pd.DataFrame] = {}
    pending = list(dict.fromkeys(tickers))
    for attempt in range(retries):
        failed = []
        for i in range(0, len(pending), chunk):
            batch = pending[i:i + chunk]
            df = yf.download(batch, period=period, interval="1d", auto_adjust=False,
                             progress=False, threads=4, group_by="ticker")
            for t in batch:
                try:
                    sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                    sub = sub[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
                except KeyError:
                    sub = pd.DataFrame()
                if len(sub):
                    out[t] = sub
                else:
                    failed.append(t)
        if not failed:
            break
        log.info("yfinance: %d tickers failed, retry %d", len(failed), attempt + 1)
        pending = failed
        time.sleep(15 * (attempt + 1))
    return out


def info(ticker: str, retries: int = 3) -> dict:
    """公司概况。批量下载后 Yahoo 常返回仅含报价字段的残缺结果，此时退避重试。"""
    best: dict = {}
    for i in range(retries):
        try:
            d = yf.Ticker(ticker).info or {}
        except Exception as e:  # noqa: BLE001
            log.warning("yfinance info %s: %s", ticker, e)
            d = {}
        if len(d) > len(best):
            best = d
        if "beta" in d or "recommendationKey" in d or "shortPercentOfFloat" in d:
            return d
        time.sleep(10 * (i + 1))
    return best


def earnings_dates(ticker: str) -> list[pd.Timestamp]:
    try:
        df = yf.Ticker(ticker).get_earnings_dates(limit=8)
        return [] if df is None else list(df.index)
    except Exception as e:  # noqa: BLE001
        log.warning("yfinance earnings %s: %s", ticker, e)
        return []


def news(ticker: str, retries: int = 2) -> list[dict]:
    for i in range(retries):
        try:
            n = yf.Ticker(ticker).news or []
            if n:
                return n
        except Exception as e:  # noqa: BLE001
            log.warning("yfinance news %s: %s", ticker, e)
        time.sleep(5 * (i + 1))
    return []


ACTION_CN = {"up": "上调评级", "down": "下调评级", "main": "维持", "init": "首次覆盖", "reit": "重申"}
PT_CN = {"Raises": "上调目标价", "Lowers": "下调目标价", "Maintains": "维持目标价", "Announces": "给出目标价"}


def analyst(ticker: str, days: int = 30, limit: int = 6) -> dict:
    """卖方评级动态（具名机构）+ 一致预期。数据：Yahoo Finance。"""
    out: dict = {"actions": [], "consensus": None, "targets": None}
    tk = yf.Ticker(ticker)
    try:
        ud = tk.upgrades_downgrades
        if ud is not None and len(ud):
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
            ud = ud[ud.index >= cutoff].sort_index(ascending=False).head(limit)
            for d, r in ud.iterrows():
                cur, prior = r.get("currentPriceTarget"), r.get("priorPriceTarget")
                out["actions"].append({
                    "date": d.strftime("%Y-%m-%d"), "firm": r["Firm"],
                    "action": ACTION_CN.get(r.get("Action"), r.get("Action")),
                    "from": r.get("FromGrade") or None, "to": r.get("ToGrade") or None,
                    "pt_action": PT_CN.get(r.get("priceTargetAction"), r.get("priceTargetAction")),
                    "target": float(cur) if cur and cur == cur and cur > 0 else None,
                    "prior": float(prior) if prior and prior == prior and prior > 0 else None,
                })
    except Exception as e:  # noqa: BLE001
        log.warning("analyst actions %s: %s", ticker, e)
    try:
        rec = tk.recommendations
        if rec is not None and len(rec):
            r0 = rec[rec["period"] == "0m"].iloc[0]
            out["consensus"] = {k: int(r0[k]) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    except Exception as e:  # noqa: BLE001
        log.warning("analyst recs %s: %s", ticker, e)
    try:
        pt = tk.analyst_price_targets
        if pt and pt.get("mean"):
            out["targets"] = {k: pt.get(k) for k in ("mean", "median", "high", "low", "current")}
    except Exception as e:  # noqa: BLE001
        log.warning("analyst targets %s: %s", ticker, e)
    return out
