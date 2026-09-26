"""英国（伦敦交易所）：FTSE 100 / 250 指数 + FTSE 350 个股异动。

Yahoo 日线常缺当日收盘竞价（16:30–16:35）后的收盘价：此时用 CNBC 批量报价补齐当日收盘，
并用 Yahoo 报价接口对指数和异动个股做交叉校验。补齐来源写入快照并在页面标注。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from . import alerts as A
from . import calendars, crossasset
from .build_us import _insight_block, _upto
from .common import ErrorLog, get_logger, iso, load_yaml, settings, utcnow
from .fetchers import cnbc, universe, yahoo
from .patch import patch_close as _patch_close

log = get_logger()


def _xcheck_yahoo(close: float | None, ticker: str, session: date, cal: str, tol: float) -> dict:
    """用 Yahoo 报价接口校验；只有当 session 仍是最近交易日时报价才可比。"""
    if close is None:
        return {"status": "na", "note": "无收盘价"}
    if calendars.latest_session(cal) != session:
        return {"status": "na", "note": "非最近交易日，无法用实时报价校验"}
    q = yahoo.quote_last(ticker)
    if not q:
        return {"status": "na", "note": "第二源无数据"}
    diff = (q["last"] / close - 1) * 100
    return {"status": "bad" if abs(diff) > tol else "ok", "other": q["last"], "diff_pct": diff, "source": q["source"],
            "time": None}


def build(session: date) -> dict:
    cfg, acfg = settings(), load_yaml("alerts.yaml")
    m = cfg["markets"]["uk"]
    tz, tol, cal = m["tz"], cfg["cross_check_tolerance_pct"], m["calendar"]
    err = ErrorLog()
    snap = {
        "market": "uk", "session": session.isoformat(), "generated_at": iso(utcnow()),
        "session_close_utc": iso(calendars.session_close(cal, session)), "exchange_tz": tz,
        "currency_note": "个股价格单位为便士（GBp）", "sections": {}, "errors": err.items,
    }

    # ---------- 指数 ----------
    idx_cfg = m["indices"]
    hist = yahoo.history([i["yahoo"] for i in idx_cfg], period="1y", keep_last_nan=True)
    try:
        cq = cnbc.quotes([i["cnbc"] for i in idx_cfg])
    except Exception as e:  # noqa: BLE001
        err.add("indices", f"CNBC 行情获取失败: {e}")
        cq = {}
    rows = []
    for i in idx_cfg:
        df, src = _patch_close(_upto({"x": hist.get(i["yahoo"])}, session)["x"] if hist.get(i["yahoo"]) is not None else None,
                               cq.get(i["cnbc"]), session, tz)
        st = A.day_stats(df, session)
        if st is None:
            err.add("indices", f"{i['name']}: 当日收盘价缺失（Yahoo 与 CNBC 均无），数据暂缺")
        row = {"name": i["name"], "ticker": i["yahoo"], "stats": st, "close_source": src,
               "xcheck": _xcheck_yahoo(st and st["close"], i["yahoo"], session, cal, tol) if src and src.startswith("CNBC")
               else _xcheck_cnbc(st, cq.get(i["cnbc"]), session, tz, tol),
               "spark": [round(float(x), 4) for x in df["Close"].iloc[-60:]] if st else None}
        if st and abs(st["pct"]) >= acfg["indices"]["pct_move"]:
            row["alert"] = f"|涨跌幅| ≥ {acfg['indices']['pct_move']}%"
            row["technicals"] = A.technicals(df)
        rows.append(row)
    snap["sections"]["indices"] = rows

    # ---------- FTSE 350 异动 ----------
    scfg = acfg["stocks"]
    sets = acfg.get("universe", {}).get("uk", ["ftse100", "ftse250"])
    try:
        uni = universe.uk(sets)
    except Exception as e:  # noqa: BLE001
        err.add("alerts", f"FTSE 成分股获取失败: {e}")
        uni = {}
    sh = _upto(yahoo.history(list(uni), period="1y", keep_last_nan=True), session) if uni else {}
    try:
        sq = cnbc.quotes([cnbc.uk_symbol(v["epic"]) for v in uni.values()]) if uni else {}
    except Exception as e:  # noqa: BLE001
        err.add("alerts", f"CNBC 个股报价失败: {e}")
        sq = {}
    patched, stats = 0, {}
    mismatched = []
    for t, meta in uni.items():
        raw = sh.get(t)
        df, src = _patch_close(raw, sq.get(cnbc.uk_symbol(meta["epic"])), session, tz)
        if src == "MISMATCH":
            # CNBC 代码对应的不是同一证券：改用 Yahoo 报价接口补当日收盘（仍须前收一致）
            mismatched.append(t)
            yq = yahoo.quote_last(t) if calendars.latest_session(cal) == session else None
            if yq:
                # session 仍是最近交易日，报价接口的最新价即该日收盘竞价价
                yq = {**yq, "time": calendars.session_close(cal, session).isoformat()}
            df, src = _patch_close(raw, yq, session, tz)
            src = "Yahoo 报价（CNBC 代码不匹配）" if src and src != "MISMATCH" else None
        sh[t] = df
        if src and src.startswith("CNBC"):
            patched += 1
        st = A.day_stats(df, session, scfg["sigma_window"])
        if st:
            stats[t] = (st, src)
    missing = len(uni) - len(stats)
    if uni and missing > 0.2 * len(uni):
        err.add("alerts", f"{missing}/{len(uni)} 只股票缺当日数据，异动结果不完整")
    hits = []
    for t, (st, src) in stats.items():
        why = A.stock_triggered(st, scfg)
        if why:
            meta = uni[t]
            hits.append({"ticker": t, "epic": meta["epic"], "name": meta["name"], "sector": meta.get("sector"),
                         "index": meta.get("index"), "stats": st, "why": why, "close_source": src})
    hits.sort(key=lambda h: -abs(h["stats"]["z"] or 0))
    if hits:
        time.sleep(15)
    for n, h in enumerate(hits):
        t = h["ticker"]
        src = h["close_source"] or ""
        if t in mismatched:
            h["xcheck"] = {"status": "na", "note": "CNBC 代码对应其他证券，无第二源"}
        elif src.startswith("CNBC"):
            h["xcheck"] = _xcheck_yahoo(h["stats"]["close"], t, session, cal, tol)
        else:
            h["xcheck"] = _xcheck_cnbc(h["stats"], sq.get(cnbc.uk_symbol(h["epic"])), session, tz, tol)
        if n >= scfg["detail_cards"]:
            continue
        h["detail"] = True
        h["technicals"] = A.technicals(sh[t])
        h["fundamentals"] = A.fundamentals(yahoo.info(t))
        h["earnings"], h["filings"] = [], []
        short = (h["fundamentals"].get("short_name") or h["name"]).split()[0].strip(",.")
        h["insight"] = _insight_block(t, [h["epic"], short, h["name"].split()[0]])
        h["analyst"] = yahoo.analyst(t)
    snap["sections"]["breadth"] = crossasset.breadth({t: v[0] for t, v in stats.items()}, uni)
    snap["sections"]["alerts"] = {
        "universe": sets, "universe_size": len(uni), "with_data": len(stats), "patched_from_cnbc": patched,
        "cnbc_symbol_mismatch": mismatched,
        "criteria": scfg, "hits": hits, "sec_enabled": False,
    }
    return snap


def _xcheck_cnbc(st: dict | None, q: dict | None, session: date, tz: str, tol: float) -> dict:
    """Yahoo 日线有收盘价时，与 CNBC 比对（同一交易日才有效）。"""
    if not st or not q or not q.get("time"):
        return {"status": "na", "note": "第二源无数据"}
    if datetime.fromisoformat(q["time"]).astimezone(ZoneInfo(tz)).date() != session:
        return {"status": "na", "note": "第二源报价日期 ≠ 交易日"}
    diff = (q["last"] / st["close"] - 1) * 100
    return {"status": "bad" if abs(diff) > tol else "ok", "other": q["last"], "diff_pct": diff, "source": q["source"],
            "time": q["time"]}


def core_ok(snap: dict) -> bool:
    return all(r["stats"] for r in snap["sections"]["indices"])
