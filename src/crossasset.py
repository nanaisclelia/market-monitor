"""跨资产（利率 / 原油 / 美元 / 人民币）与市场宽度。

- 收益率按 bp 计变动，σ 为过去 20 日每日 bp 变动的标准差；价格类按 % 计。
- 双源校验：Yahoo 对 CNBC（报价须为同一交易日）。期货若与第二源「次月合约」吻合，
  判定为合约换月：跨合约涨跌不可比，交由发布闸门隔离。
"""
from __future__ import annotations

from datetime import date, datetime
from statistics import median
from zoneinfo import ZoneInfo

import pandas as pd

from .common import get_logger
from .fetchers import cnbc, yahoo

log = get_logger()

ASSETS = [
    {"key": "us10y", "name": "美债 10Y", "name_en": "US 10Y", "yahoo": "^TNX", "cnbc": "US10Y", "kind": "yield", "stress": 1},
    {"key": "wti", "name": "WTI 原油", "name_en": "WTI crude", "yahoo": "CL=F", "cnbc": "@CL.1", "roll": "@CL.2", "kind": "price", "stress": 1},
    {"key": "brent", "name": "Brent 原油", "name_en": "Brent crude", "yahoo": "BZ=F", "cnbc": "@LCO.1", "roll": "@LCO.2", "kind": "price", "stress": 1},
    {"key": "dxy", "name": "美元指数", "name_en": "Dollar index", "yahoo": "DX-Y.NYB", "cnbc": ".DXY", "kind": "price", "stress": 1},
    {"key": "usdcny", "name": "USD/CNY", "name_en": "USD/CNY", "yahoo": "CNY=X", "cnbc": "CNY=", "kind": "price", "stress": 0},
]
QUOTE_ONLY = [{"key": "usdcnh", "name": "USD/CNH", "name_en": "USD/CNH", "cnbc": "CNH="}]


def _same_day(q: dict | None, session: date, tz: str) -> bool:
    return bool(q and q.get("time")) and datetime.fromisoformat(q["time"]).astimezone(ZoneInfo(tz)).date() == session


def build(session: date, tz: str = "America/New_York", window: int = 20) -> list[dict]:
    hist = yahoo.history([a["yahoo"] for a in ASSETS], period="6mo")
    syms = [a["cnbc"] for a in ASSETS] + [a["roll"] for a in ASSETS if a.get("roll")] + [q["cnbc"] for q in QUOTE_ONLY]
    try:
        cq = cnbc.quotes(syms)
    except Exception as e:  # noqa: BLE001
        log.warning("cross-asset CNBC: %s", e)
        cq = {}
    out = []
    for a in ASSETS:
        row = {k: a[k] for k in ("key", "name", "name_en", "kind", "stress")}
        df = hist.get(a["yahoo"])
        if df is None or not len(df):
            row["error"] = "无数据"
            out.append(row)
            continue
        df = df[df.index.date <= session]
        c = df["Close"]
        if c.index[-1].date() != session or len(c) < window + 2:
            row["error"] = f"最新数据 {c.index[-1].date()} ≠ {session}"
            out.append(row)
            continue
        if a["kind"] == "yield":
            d = c.diff() * 100                        # bp
            chg, unit = float(d.iloc[-1]), "bp"
        else:
            d = c.pct_change() * 100
            chg, unit = float(d.iloc[-1]), "%"
        sig = float(d.iloc[-window - 1:-1].std())
        row.update({"close": float(c.iloc[-1]), "prev": float(c.iloc[-2]), "chg": chg, "unit": unit,
                    "sigma": sig, "z": chg / sig if sig else None, "spark": [round(float(x), 4) for x in c.iloc[-60:]],
                    "source": "Yahoo Finance"})
        q = cq.get(a["cnbc"])
        if _same_day(q, session, tz):
            if a["kind"] == "yield":
                diff_bp = (q["last"] - row["close"]) * 100
                row["xcheck"] = {"status": "ok" if abs(diff_bp) <= 3 else "bad", "other": q["last"], "diff_bp": diff_bp,
                                 "source": q["source"]}
            else:
                diff = (q["last"] / row["close"] - 1) * 100
                row["xcheck"] = {"status": "ok" if abs(diff) <= 0.5 else "bad", "other": q["last"], "diff_pct": diff,
                                 "source": q["source"]}
        else:
            row["xcheck"] = {"status": "na", "note": "第二源无当日报价"}
        # 合约换月：Yahoo 当日价与第二源次月合约吻合，而与前一日（近月）不可比
        rq = cq.get(a.get("roll") or "")
        if rq and rq.get("last") and abs(rq["last"] / row["close"] - 1) < 0.003 and (row["xcheck"]["status"] != "ok"):
            row["roll"] = {"next_contract": rq["last"], "note": "Yahoo 当日切换到次月合约，与前一日近月价格不可比"}
        out.append(row)
    for qo in QUOTE_ONLY:
        q = cq.get(qo["cnbc"])
        out.append({"key": qo["key"], "name": qo["name"], "name_en": qo["name_en"], "kind": "quote",
                    "close": q["last"] if q else None, "chg": q.get("change_pct") if q else None, "unit": "%",
                    "quote_only": True, "source": "CNBC quote",
                    "error": None if _same_day(q, session, tz) else "无当日报价"})
    return out


def breadth(stats: dict[str, dict], meta: dict[str, dict]) -> dict:
    """上涨 / 下跌家数与行业扩散度（行业涨跌幅中位数为正的行业占比）。"""
    pcts = {t: s["pct"] for t, s in stats.items()}
    adv = sum(p > 0 for p in pcts.values())
    dec = sum(p < 0 for p in pcts.values())
    by_sec: dict[str, list[float]] = {}
    for t, p in pcts.items():
        sec = (meta.get(t) or {}).get("sector")
        if isinstance(sec, str) and sec.strip() and not sec.startswith("主题"):
            by_sec.setdefault(sec.strip().title(), []).append(p)
    secs = {k: median(v) for k, v in by_sec.items() if len(v) >= 3}
    return {"n": len(pcts), "adv": adv, "dec": dec, "unch": len(pcts) - adv - dec,
            "pct_adv": adv / len(pcts) * 100 if pcts else None,
            "sectors": sorted(({"name": k, "median": v, "n": len(by_sec[k])} for k, v in secs.items()), key=lambda x: -x["median"]),
            "sectors_up": sum(v > 0 for v in secs.values()), "sectors_total": len(secs)}
