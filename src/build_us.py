"""美股 + 贵金属 + 异动：抓取、校验、生成快照。"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import alerts as A
from . import calendars, crossasset, insights, themes
from .common import ErrorLog, get_logger, iso, load_yaml, settings, utcnow
from .fetchers import cnbc, lbma, news, universe, yahoo

log = get_logger()


def _xcheck(primary: float | None, q: dict | None, session: date, tz: str, tol: float) -> dict:
    """与 CNBC 比对；CNBC 报价日期须为同一交易日才算有效校验。"""
    if primary is None or not q:
        return {"status": "na", "note": "第二源无数据"}
    t = datetime.fromisoformat(q["time"]).astimezone(ZoneInfo(tz)) if q.get("time") else None
    if not t or t.date() != session:
        return {"status": "na", "note": f"第二源报价日期 {t.date() if t else '未知'} ≠ 交易日"}
    diff = (q["last"] / primary - 1) * 100
    return {"status": "bad" if abs(diff) > tol else "ok", "other": q["last"], "diff_pct": diff,
            "source": q["source"], "time": q["time"]}


def _upto(h: dict, session: date) -> dict:
    """截断到交易日（回补历史日期时使用；当日运行时无影响）。"""
    return {t: df[df.index.date <= session] for t, df in h.items()}


def _insight_block(ticker: str, keywords: list[str], extra=None, **kw) -> dict:
    try:
        return insights.for_target(ticker, keywords, extra=extra, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("insights %s: %s", ticker, e)
        return {"insights": [], "articles_scanned": 0, "headlines": [], "error": str(e)}


def build(session: date) -> dict:
    cfg, acfg = settings(), load_yaml("alerts.yaml")
    m = cfg["markets"]["us"]
    tz, tol = m["tz"], cfg["cross_check_tolerance_pct"]
    err = ErrorLog()
    close_utc = calendars.session_close(m["calendar"], session)
    snap = {
        "market": "us", "session": session.isoformat(), "generated_at": iso(utcnow()),
        "session_close_utc": iso(close_utc), "exchange_tz": tz, "sections": {}, "errors": err.items,
    }

    # ---------- 指数 ----------
    idx_cfg = m["indices"]
    hist = _upto(yahoo.history([i["yahoo"] for i in idx_cfg] + [x["yahoo"] for x in cfg["metals"]], period="1y"), session)
    try:
        cq = cnbc.quotes([i["cnbc"] for i in idx_cfg] + [x["cnbc"] for x in cfg["metals"]])
    except Exception as e:  # noqa: BLE001
        err.add("cross-check", f"CNBC 行情获取失败: {e}")
        cq = {}
    rows = []
    for i in idx_cfg:
        df = hist.get(i["yahoo"])
        st = A.day_stats(df, session)
        if st is None:
            last = df.index[-1].date() if df is not None and len(df) else "无"
            err.add("indices", f"{i['name']}: Yahoo 最新 K 线日期 {last}，非 {session}，数据暂缺")
        row = {"name": i["name"], "ticker": i["yahoo"], "stats": st, "source": "Yahoo Finance (yfinance)",
               "xcheck": _xcheck(st and st["close"], cq.get(i["cnbc"]), session, tz, tol),
               "spark": [round(float(x), 4) for x in df["Close"].iloc[-60:]] if st else None}
        if st and i.get("alert", True) and abs(st["pct"]) >= acfg["indices"]["pct_move"]:
            row["alert"] = f"|涨跌幅| ≥ {acfg['indices']['pct_move']}%"
            row["technicals"] = A.technicals(df)
            row["insight"] = _insight_block(i["yahoo"], ["S&P", "Nasdaq", "Dow", "stocks", "equities", "Wall Street"])
        rows.append(row)
    snap["sections"]["indices"] = rows

    # ---------- 贵金属 ----------
    mrows = []
    mthr, trig_src = acfg["metals"], acfg["metals"]["trigger_source"]
    for x in cfg["metals"]:
        row = {"name": x["name"], "key": x["key"], "threshold": mthr[x["key"]]}
        # LBMA 定价：只有定价日期 == 交易日才参与触发，旧定价明确标注日期
        try:
            fx = [f for f in lbma.fixes(x["lbma"]) if f[0] <= session.isoformat()]
            (d1, p1), (d0, p0) = fx[-1], fx[-2]
            row["lbma"] = {"date": d1, "price": p1, "prev_date": d0, "prev": p0, "pct": (p1 / p0 - 1) * 100,
                           "fix_time_london": lbma.FIX_TIME[x["lbma"]], "current": d1 == session.isoformat(),
                           "source": f"LBMA {x['lbma']} (prices.lbma.org.uk)"}
        except Exception as e:  # noqa: BLE001
            err.add("metals", f"{x['name']} LBMA 定价获取失败: {e}")
        df = hist.get(x["yahoo"])
        st = A.day_stats(df, session)
        if st is None:
            err.add("metals", f"{x['name']} 期货 {x['yahoo']}: 当日 K 线缺失")
        else:
            row["futures"] = {**st, "ticker": x["yahoo"], "spark": [round(float(v), 4) for v in df["Close"].iloc[-60:]], "source": "COMEX/NYMEX 近月期货 (Yahoo)",
                              "xcheck": _xcheck(st["close"], cq.get(x["cnbc"]), session, tz, tol)}
        lb = row.get("lbma") if row.get("lbma", {}).get("current") else None
        fut = row.get("futures")
        hit_l = lb is not None and abs(lb["pct"]) >= row["threshold"]
        hit_f = fut is not None and abs(fut["pct"]) >= row["threshold"]
        hit = {"lbma": hit_l, "futures": hit_f, "either": hit_l or hit_f}[trig_src]
        if hit:
            row["alert"] = (f"|涨跌幅| ≥ {row['threshold']}%（"
                            + "、".join(s for s, h in (("LBMA", hit_l), ("期货", hit_f)) if h) + "）")
            if df is not None:
                row["technicals"] = A.technicals(df)
            kw = x["name"].split()[-1]
            try:
                kit = news.kitco_stories([kw.lower()] + (["pgm", "platinum-group"] if x["key"] in ("platinum", "palladium") else []))
            except Exception as e:  # noqa: BLE001
                err.add("metals", f"Kitco 新闻获取失败: {e}")
                kit = []
            row["insight"] = _insight_block(x["yahoo"], [kw, "bullion", "precious metal"], extra=kit, max_articles=8)
        mrows.append(row)
    snap["sections"]["metals"] = mrows

    # ---------- 个股异动 ----------
    try:
        uni = universe.us(acfg["universe"]["us"])
        for m in (m for t in themes.config() for m in t["members"] if m["market"] == "us"):
            uni.setdefault(m["ticker"], {"ticker": m["ticker"], "name": m["name"], "sector": "主题：" + m["role"]})
    except Exception as e:  # noqa: BLE001
        err.add("alerts", f"标的池获取失败: {e}")
        uni = {}
    scfg = acfg["stocks"]
    sh = _upto(yahoo.history(list(uni), period="1y"), session) if uni else {}
    missing = [t for t in uni if A.day_stats(sh.get(t), session, scfg["sigma_window"]) is None]
    if uni and len(missing) > 0.2 * len(uni):
        err.add("alerts", f"{len(missing)}/{len(uni)} 只股票缺当日数据，异动结果不完整")
    # ---------- 主题追踪（CPO 等）：美股成员复用上面的日线，A股成员单独下载 ----------
    cn_t = [m["ticker"] for t in themes.config() for m in t["members"] if m["market"] == "cn"]
    th_hist = {**sh, **(yahoo.history(cn_t, period="1y") if cn_t else {})}
    try:
        snap["sections"]["themes"] = themes.build(session, th_hist, scfg)
        for th in snap["sections"]["themes"]:
            for r in th["members"]:
                if r.get("error"):
                    err.add("themes", f"{th['name']} · {r['ticker']}: {r['error']}")
    except Exception as e:  # noqa: BLE001
        err.add("themes", f"主题追踪失败: {e}")
        snap["sections"]["themes"] = []

    hits, all_stats = [], {}
    for t, meta in uni.items():
        st = A.day_stats(sh.get(t), session, scfg["sigma_window"])
        if st is None:
            continue
        all_stats[t] = st
        why = A.stock_triggered(st, scfg)
        if why:
            hits.append({"ticker": t, "name": meta["name"], "sector": meta.get("sector"), "stats": st, "why": why})
    hits.sort(key=lambda h: -abs(h["stats"]["z"] or 0))

    try:
        sq = cnbc.quotes([cnbc.yahoo_to_cnbc(h["ticker"]) for h in hits]) if hits else {}
    except Exception as e:  # noqa: BLE001
        err.add("cross-check", f"CNBC 个股报价失败: {e}")
        sq = {}
    ua = cfg.get("sec_user_agent") or ""
    if hits:
        time.sleep(20)  # 批量下载后给 Yahoo 限流窗口降温，再逐只取概况/新闻
    for n, h in enumerate(hits):
        t = h["ticker"]
        h["xcheck"] = _xcheck(h["stats"]["close"], sq.get(cnbc.yahoo_to_cnbc(t)), session, tz, tol)
        if n >= scfg["detail_cards"]:
            continue
        h["detail"] = True
        h["technicals"] = A.technicals(sh[t])
        h["fundamentals"] = A.fundamentals(yahoo.info(t))
        if h["fundamentals"].get("beta") is None and h["fundamentals"].get("rec_key") is None:
            err.add("alerts", f"{t}: Yahoo 公司概况不完整（可能限流），部分风险指标暂缺")
        # 已证实驱动：财报日、SEC 公告
        ed = [d for d in yahoo.earnings_dates(t)
              if session - timedelta(days=3) <= d.tz_convert(tz).date() <= session]
        h["earnings"] = [d.tz_convert(tz).strftime("%Y-%m-%d %H:%M %Z") for d in ed]
        try:
            h["filings"] = news.sec_filings(t, session, ua)
        except Exception as e:  # noqa: BLE001
            err.add("drivers", f"{t} SEC 公告获取失败: {e}")
            h["filings"] = []
        short = (h["fundamentals"].get("short_name") or h["name"]).split()[0].strip(",.")
        h["insight"] = _insight_block(t, [t, short, h["name"].split()[0]])
        h["analyst"] = yahoo.analyst(t)
    snap["sections"]["breadth"] = crossasset.breadth(all_stats, uni)
    try:
        snap["sections"]["cross"] = crossasset.build(session, tz)
    except Exception as e:  # noqa: BLE001
        err.add("cross", f"跨资产数据失败: {e}")
        snap["sections"]["cross"] = []
    snap["sections"]["alerts"] = {
        "universe": acfg["universe"]["us"], "universe_size": len(uni), "with_data": len(uni) - len(missing),
        "criteria": scfg, "hits": hits, "sec_enabled": bool(ua),
    }
    return snap


def core_ok(snap: dict) -> bool:
    """核心数据（主要指数）齐全才视为完成，否则由自动调度继续重试。"""
    return all(r["stats"] for r in snap["sections"]["indices"])
