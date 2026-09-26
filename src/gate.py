"""数据发布闸门：在渲染前对每个市场快照做自动核验，失败时降级为「待核验」而不是正常展示。

检查项
  - 指数完整性：主要指数必须有当日收盘，且双源校验不能「不一致」
  - 覆盖率：监控池当日有效数据占比（< 90% 警告，< 80% 失败）
  - 异常值：|涨跌幅| ≥ 40% 的标的，必须双源一致才放行，否则隔离
  - 企业行为 / 复权：价格比接近常见拆并股比例（1:2、1:3、1:5、1:10 及其倒数）且无第二源确认 → 隔离
  - 币种 / 单位：价格比接近 100 或 1/100（便士与英镑混用）→ 隔离
  - 双源对账：异动标的中「不一致」的直接隔离；「无第二源」计入警告
  - 代码映射：记录被识别为映射错误并已替换来源的数量
"""
from __future__ import annotations

SPLIT_RATIOS = (2, 3, 4, 5, 10, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10)


def _ratio_flag(close: float, prev: float) -> str | None:
    if not prev:
        return None
    r = close / prev
    if any(abs(r - k) / k < 0.03 for k in (100, 1 / 100)):
        return "币种 / 单位疑似混用（约 100 倍）"
    if any(abs(r - k) / k < 0.02 for k in SPLIT_RATIOS):
        return "疑似拆并股 / 复权（价格比接近整数倍）"
    return None


def check(snap: dict | None) -> dict | None:
    if not snap:
        return None
    checks, quarantine = [], {}
    idx = snap["sections"].get("indices", [])
    miss = [r["name"] for r in idx if not r.get("stats")]
    bad = [r["name"] for r in idx if (r.get("xcheck") or {}).get("status") == "bad"]
    unv = [r["name"] for r in idx if r.get("stats") and (r.get("xcheck") or {}).get("status") != "ok"]
    checks.append({"name": "指数完整性", "name_en": "Index completeness",
                   "status": "fail" if miss or bad else ("warn" if unv else "pass"),
                   "detail": ("缺失：" + "、".join(miss) + "；" if miss else "") + ("双源不一致：" + "、".join(bad) + "；" if bad else "")
                   + ("未完成双源校验：" + "、".join(unv) if unv else "") or f"{len(idx)} 个指数均有收盘并通过双源校验",
                   "detail_en": f"{len(idx) - len(miss)}/{len(idx)} indices present; {len(bad)} mismatched; {len(unv)} unverified"})

    al = snap["sections"].get("alerts") or {}
    if al:
        n, got = al.get("universe_size", 0), al.get("with_data", 0)
        cov = got / n if n else 0
        checks.append({"name": "覆盖率", "name_en": "Coverage", "status": "fail" if cov < 0.8 else ("warn" if cov < 0.9 else "pass"),
                       "detail": f"{got}/{n}（{cov:.1%}）", "detail_en": f"{got}/{n} ({cov:.1%})"})
        noxc = 0
        for h in al.get("hits", []):
            st, xs = h["stats"], (h.get("xcheck") or {}).get("status")
            reason = None
            if xs == "bad":
                reason = "双源不一致"
            elif abs(st["pct"]) >= 40 and xs != "ok":
                reason = "涨跌幅 ≥ 40% 且无第二源确认"
            elif (flag := _ratio_flag(st["close"], st["prev_close"])) and xs != "ok":
                reason = flag
            if xs != "ok":
                noxc += 1
            if reason:
                quarantine[h["ticker"]] = reason
        checks.append({"name": "异常值 / 企业行为 / 币种", "name_en": "Outliers / corporate actions / units",
                       "status": "warn" if quarantine else "pass",
                       "detail": f"隔离 {len(quarantine)} 只：" + "；".join(f"{k} {v}" for k, v in quarantine.items()) if quarantine else "未发现",
                       "detail_en": f"{len(quarantine)} quarantined" if quarantine else "none found"})
        hits = len(al.get("hits", []))
        checks.append({"name": "异动双源对账", "name_en": "Mover reconciliation",
                       "status": "warn" if noxc else "pass",
                       "detail": f"{hits - noxc}/{hits} 只双源一致" + (f"，{noxc} 只无第二源" if noxc else ""),
                       "detail_en": f"{hits - noxc}/{hits} reconciled"})
        mm = al.get("cnbc_symbol_mismatch") or []
        if "cnbc_symbol_mismatch" in al:
            checks.append({"name": "代码映射", "name_en": "Symbol mapping", "status": "pass" if not mm else "warn",
                           "detail": f"{len(mm)} 只第二源代码映射到其他证券，已改用备用来源：" + "、".join(mm) if mm else "无映射错误",
                           "detail_en": f"{len(mm)} remapped" if mm else "no mapping errors"})
    cross = snap["sections"].get("cross") or []
    if cross:
        rolled = [r["name"] for r in cross if r.get("roll")]
        badx = [r["name"] for r in cross if (r.get("xcheck") or {}).get("status") == "bad" and not r.get("roll")]
        for r in cross:
            if r.get("roll"):
                quarantine[r["key"]] = "期货合约换月：跨合约涨跌不可比"
            elif (r.get("xcheck") or {}).get("status") == "bad":
                quarantine[r["key"]] = "双源不一致"
        checks.append({"name": "跨资产对账 / 合约换月", "name_en": "Cross-asset reconciliation / contract rolls",
                       "status": "warn" if rolled or badx else "pass",
                       "detail": ("合约换月已隔离：" + "、".join(rolled) + "；" if rolled else "") + ("双源不一致：" + "、".join(badx) if badx else "")
                       or f"{len(cross)} 项通过", "detail_en": f"{len(rolled)} roll(s) quarantined, {len(badx)} mismatched"})
    br = snap["sections"].get("breadth")
    if br is not None:
        n_uni = (snap["sections"].get("alerts") or {}).get("universe_size") or br.get("n") or 0
        ok_b = br.get("n", 0) >= 0.8 * n_uni if n_uni else False
        checks.append({"name": "市场宽度样本", "name_en": "Breadth sample", "status": "pass" if ok_b else "warn",
                       "detail": f"{br.get('n', 0)}/{n_uni} 只" + (f"（{br['source']}）" if br.get("source") else ""),
                       "detail_en": f"{br.get('n', 0)}/{n_uni}"})
    worst = "fail" if any(c["status"] == "fail" for c in checks) else ("warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"status": worst, "checks": checks, "quarantine": quarantine}
