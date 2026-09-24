"""异动检测与基于数据的风险指标（不含任何主观判断文字）。"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd


def _pct(a, b):
    return None if not b or b != b else (a / b - 1) * 100


def day_stats(df: pd.DataFrame, session: date, window: int = 20) -> dict | None:
    """session 当日的收盘统计；若最新一根 K 线不是 session（未结算/停牌），返回 None。"""
    if df is None or len(df) < window + 2 or df.index[-1].date() != session:
        return None
    c, v = df["Close"], df["Volume"]
    rets = c.pct_change() * 100
    sigma = rets.iloc[-window - 1:-1].std()
    pct = rets.iloc[-1]
    avgv = v.iloc[-window - 1:-1].mean()
    return {
        "close": float(c.iloc[-1]), "prev_close": float(c.iloc[-2]), "pct": float(pct),
        "sigma20": float(sigma) if sigma == sigma else None,
        "z": float(pct / sigma) if sigma and sigma == sigma else None,
        "volume": float(v.iloc[-1]),
        # 当日成交量为 0 通常是数据源尚未入库，不计算量比
        "rel_volume": float(v.iloc[-1] / avgv) if avgv and v.iloc[-1] > 0 else None,
        "avg_dollar_vol20": float((c * v).iloc[-window - 1:-1].mean()),
    }


def stock_triggered(st: dict, cfg: dict) -> list[str]:
    why = []
    if abs(st["pct"]) >= cfg["pct_move"]:
        why.append(f"|涨跌幅| ≥ {cfg['pct_move']}%")
    if st["z"] is not None and abs(st["z"]) >= cfg["sigma_multiple"] and abs(st["pct"]) >= cfg.get("sigma_min_abs_pct", 0):
        why.append(f"|涨跌幅| ≥ {cfg['sigma_multiple']}σ（20日σ={st['sigma20']:.2f}%）")
    return why


def _rsi(c: pd.Series, n: int = 14) -> float | None:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    r = 100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1]) if dn.iloc[-1] else 100.0
    return None if r != r else float(r)


def technicals(df: pd.DataFrame) -> dict:
    c = df["Close"]
    last = c.iloc[-1]
    out = {}
    for n in (20, 50, 200):
        if len(c) >= n:
            ma = c.iloc[-n:].mean()
            out[f"ma{n}"] = float(ma)
            out[f"vs_ma{n}_pct"] = float(_pct(last, ma))
    hi, lo = df["High"].iloc[-252:].max(), df["Low"].iloc[-252:].min()
    out.update({
        "high_52w": float(hi), "low_52w": float(lo),
        "from_high_pct": float(_pct(last, hi)), "from_low_pct": float(_pct(last, lo)),
        "rsi14": _rsi(c),
        "prev_high": float(df["High"].iloc[-2]), "prev_low": float(df["Low"].iloc[-2]),
        "gap_pct": float(_pct(df["Open"].iloc[-1], c.iloc[-2])),
        "streak": _streak(c),
    })
    return out


def _streak(c: pd.Series) -> int:
    """连续同向天数（正=连涨，负=连跌）。"""
    s = np.sign(c.diff().dropna().values[::-1])
    if not len(s) or s[0] == 0:
        return 0
    n = 0
    for x in s:
        if x != s[0]:
            break
        n += 1
    return int(n * s[0])


def fundamentals(info: dict) -> dict:
    keys = {
        "market_cap": "marketCap", "trailing_pe": "trailingPE", "forward_pe": "forwardPE",
        "ps": "priceToSalesTrailing12Months", "pb": "priceToBook", "beta": "beta",
        "short_pct_float": "shortPercentOfFloat", "short_ratio": "shortRatio",
        "float_shares": "floatShares", "target_mean": "targetMeanPrice",
        "rec_key": "recommendationKey", "n_analysts": "numberOfAnalystOpinions",
        "short_name": "shortName", "sector": "sector", "industry": "industry",
    }
    out = {}
    for k, src in keys.items():
        v = info.get(src)
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            v = None
        out[k] = v
    return out
