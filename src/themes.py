"""主题追踪（如 CPO）：成员多周期表现、走势迷你图、等权主题收益、异动标记。"""
from __future__ import annotations

from datetime import date

import pandas as pd

from . import alerts as A
from . import calendars, insights
from .common import get_logger, load_yaml
from .fetchers import yahoo

log = get_logger()
CAL = {"us": "XNYS", "cn": "XSHG"}


def config() -> list[dict]:
    return load_yaml("themes.yaml").get("themes", [])


def us_tickers() -> list[str]:
    return [m["ticker"] for t in config() for m in t["members"] if m["market"] == "us"]


def _ret(c: pd.Series, n: int):
    return float((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100) if len(c) > n else None


def _limit_hit(ticker: str, pct: float) -> str | None:
    """A股涨跌停（创业板/科创板 20%，主板 10%）。"""
    code = ticker.split(".")[0]
    lim = 20 if code.startswith(("300", "301", "688")) else 10
    if abs(pct) >= lim - 0.2:
        return "涨停" if pct > 0 else "跌停"
    return None


def member_stats(m: dict, df: pd.DataFrame | None, us_session: date, scfg: dict) -> dict:
    row = {**m}
    if df is None or not len(df):
        row["error"] = "无数据"
        return row
    session = us_session if m["market"] == "us" else calendars.latest_session_on_or_before(CAL["cn"], us_session)
    df = df[df.index.date <= session]
    st = A.day_stats(df, session, scfg["sigma_window"])
    if st is None:
        row["error"] = f"最新 K 线 {df.index[-1].date() if len(df) else '无'} ≠ {session}"
        return row
    c = df["Close"]
    ytd = c[c.index.year == session.year]
    row.update({
        "session": session.isoformat(), "stats": st,
        "r5": _ret(c, 5), "r20": _ret(c, 20),
        "ytd": float((c.iloc[-1] / ytd.iloc[0] - 1) * 100) if len(ytd) > 1 else None,
        "vs_ma50": float((c.iloc[-1] / c.iloc[-50:].mean() - 1) * 100) if len(c) >= 50 else None,
        "spark": [round(float(x), 4) for x in c.iloc[-60:]],
        "why": A.stock_triggered(st, scfg),
    })
    if m["market"] == "cn" and (lim := _limit_hit(m["ticker"], st["pct"])):
        row["why"].append(lim)
    return row


def build(us_session: date, hist: dict, scfg: dict) -> list[dict]:
    out = []
    for t in config():
        rows = [member_stats(m, hist.get(m["ticker"]), us_session, scfg) for m in t["members"]]
        ok = [r for r in rows if r.get("stats")]

        def avg(key):
            # 1 日收益只统计与美股同一交易日的成员（A股休市时不把前一交易日的涨跌算进今天）
            rows_ = [r for r in ok if r["session"] == us_session.isoformat()] if key == "d1" else ok
            vals = [r["stats"]["pct"] if key == "d1" else r.get(key) for r in rows_]
            vals = [v for v in vals if v is not None]
            return sum(vals) / len(vals) if vals else None

        ins = {"insights": [], "articles_scanned": 0, "headlines": []}
        for a in t.get("insight_anchors", []):
            if len(ins["insights"]) >= 4:
                break
            try:
                b = insights.for_target(a, t["keywords"], max_articles=3, max_quotes=2)
            except Exception as e:  # noqa: BLE001
                log.warning("theme insight %s: %s", a, e)
                continue
            ins["articles_scanned"] += b["articles_scanned"]
            seen = {(q["who"], q["quote"][:60]) for q in ins["insights"]}
            ins["insights"] += [q for q in b["insights"] if (q["who"], q["quote"][:60]) not in seen]
        # 成员近 7 日卖方评级动态（具名机构），按日期倒序
        acts = []
        for m in t["members"]:
            if m["market"] != "us":
                continue
            try:
                for a in yahoo.analyst(m["ticker"], days=7, limit=4)["actions"]:
                    acts.append({**a, "ticker": m["ticker"]})
            except Exception as e:  # noqa: BLE001
                log.warning("theme analyst %s: %s", m["ticker"], e)
        acts.sort(key=lambda a: a["date"], reverse=True)
        out.append({
            "key": t["key"], "name_en": t.get("name_en", t["name"]), "analyst": {"actions": acts[:10], "consensus": None, "targets": None}, "name": t["name"], "members": rows,
            "agg": {"d1": avg("d1"), "r5": avg("r5"), "r20": avg("r20"), "ytd": avg("ytd"),
                    "up": sum(1 for r in ok if r["stats"]["pct"] > 0 and r["session"] == us_session.isoformat()),
                    "down": sum(1 for r in ok if r["stats"]["pct"] < 0 and r["session"] == us_session.isoformat()),
                    "n_today": sum(1 for r in ok if r["session"] == us_session.isoformat()),
                    "n": len(ok), "total": len(rows)},
            "flagged": [r["ticker"] for r in ok if r["why"]],
            "insight": ins,
        })
    return out
