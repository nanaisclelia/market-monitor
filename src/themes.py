"""主题追踪（CPO、TMT、周期商品）：成员多周期表现、走势迷你图、等权主题收益、异动标记、商品期货。

成员可来自美股 / 英股 / A股。每个成员按其所在交易所的最近交易日取数；与美股当日不同交易日的成员
（休市）在页面单列、不参与当日汇总。Yahoo 日线缺当日收盘时，用 CNBC 报价补齐（前收一致才采用）。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from . import alerts as A
from . import calendars, crossasset, insights
from .common import get_logger, load_yaml
from .fetchers import cnbc, yahoo
from .patch import patch_close

log = get_logger()
CAL = {"us": "XNYS", "cn": "XSHG", "uk": "XLON"}
TZ = {"us": "America/New_York", "cn": "Asia/Shanghai", "uk": "Europe/London"}


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


def _cnbc_symbol(m: dict) -> str | None:
    if m["market"] == "us":
        return cnbc.yahoo_to_cnbc(m["ticker"])
    if m["market"] == "uk":
        return cnbc.uk_symbol(m["ticker"][:-2].replace("-", "."))
    return None


def member_stats(m: dict, df: pd.DataFrame | None, q: dict | None, us_session: date, scfg: dict) -> dict:
    row = {**m}
    if df is None or not len(df):
        row["error"] = "无数据"
        return row
    session = calendars.latest_session_on_or_before(CAL[m["market"]], us_session)
    df = df[df.index.date <= session]
    raw = df
    df, src = patch_close(raw, q, session, TZ[m["market"]])
    need = src in (None, "MISMATCH") and m["market"] in ("us", "uk") and not (len(df) and df.index[-1].date() == session)
    if need and calendars.latest_session(CAL[m["market"]]) == session:
        # 第二源代码映射到其他证券（如 BP-GB）：改用 Yahoo 报价接口，仍须前收一致
        yq = yahoo.quote_last(m["ticker"])
        if yq:
            yq = {**yq, "time": calendars.session_close(CAL[m["market"]], session).isoformat()}
            df, src = patch_close(raw, yq, session, TZ[m["market"]])
            src = "Yahoo 报价（第二源无报价或代码不匹配）" if src and src != "MISMATCH" else None
    row["close_source"] = None if src in (None, "MISMATCH") else src
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


def build(us_session: date, hist: dict | None, scfg: dict) -> list[dict]:
    """hist 参数保留兼容；主题成员一律单独下载（保留缺失收盘的最后一行以便补齐）。"""
    cfg = config()
    members = [m for t in cfg for m in t["members"]]
    th_hist = yahoo.history(list(dict.fromkeys(m["ticker"] for m in members)), period="1y", keep_last_nan=True)
    syms = [s for m in members if (s := _cnbc_symbol(m))]
    try:
        cq = cnbc.quotes(list(dict.fromkeys(syms))) if syms else {}
    except Exception as e:  # noqa: BLE001
        log.warning("theme CNBC quotes: %s", e)
        cq = {}
    out = []
    for t in cfg:
        rows = [member_stats(m, th_hist.get(m["ticker"]), cq.get(_cnbc_symbol(m) or ""), us_session, scfg)
                for m in t["members"]]
        ok = [r for r in rows if r.get("stats")]
        today = [r for r in ok if r["session"] == us_session.isoformat()]

        def avg(key, rows_):
            vals = [r["stats"]["pct"] if key == "d1" else r.get(key) for r in rows_]
            vals = [v for v in vals if v is not None]
            return sum(vals) / len(vals) if vals else None

        futures = []
        if t.get("futures"):
            try:
                futures = crossasset.build(us_session, assets=t["futures"], quote_only=[])
            except Exception as e:  # noqa: BLE001
                log.warning("theme futures %s: %s", t["key"], e)

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
            "key": t["key"], "name": t["name"], "name_en": t.get("name_en", t["name"]),
            "col_label": t.get("col_label", "细分"), "col_label_en": t.get("col_label_en", "Segment"),
            "analyst": {"actions": acts[:10], "consensus": None, "targets": None}, "members": rows, "futures": futures,
            "agg": {"d1": avg("d1", today), "r5": avg("r5", today), "r20": avg("r20", today), "ytd": avg("ytd", today),
                    "up": sum(r["stats"]["pct"] > 0 for r in today), "down": sum(r["stats"]["pct"] < 0 for r in today),
                    "n_today": len(today), "n": len(ok), "total": len(rows)},
            "flagged": [r["ticker"] for r in ok if r["why"]],
            "insight": ins,
        })
    return out
