"""新闻正文抓取（Yahoo Finance 新闻流 → 原文页面）与 SEC EDGAR 公告。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from ..common import CACHE, get_logger, http_get, read_json, utcnow, write_json
from . import yahoo

log = get_logger()


def recent_stories(ticker: str, hours: int = 48, limit: int = 10) -> list[dict]:
    """[{title, url, provider, published}]，仅文字报道，按时间倒序。"""
    cutoff = utcnow() - timedelta(hours=hours)
    out = []
    for it in yahoo.news(ticker):
        c = it.get("content") or {}
        if c.get("contentType") != "STORY":
            continue
        url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
        try:
            pub = datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if not url or pub < cutoff:
            continue
        out.append({"title": c.get("title"), "url": url,
                    "provider": (c.get("provider") or {}).get("displayName"),
                    "published": pub.isoformat()})
    return out[:limit]


# 付费墙站点：直接跳过，节省时间（标题仍在「未证实」新闻列表中显示）
PAYWALLED = ("wsj.com", "barrons.com", "bloomberg.com", "ft.com", "economist.com", "marketwatch.com/articles")


def kitco_stories(keywords: list[str], days: int = 2, limit: int = 10) -> list[dict]:
    """Kitco 新闻（贵金属专门来源）：从 /news/digest 页面数据中取近 N 日文章。"""
    import re
    html = http_get("https://www.kitco.com/news/digest").text
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    paths = set(re.findall(r'"(?:urlAlias|url|path)":"(/news/article/(\d{4}-\d{2}-\d{2})/([^"]+))"', m.group(1)))
    cutoff = (utcnow() - timedelta(days=days)).date().isoformat()
    kw = [k.lower() for k in keywords]
    out = []
    for path, d, slug in sorted(paths, key=lambda x: x[1], reverse=True):
        if d < cutoff or not any(k in slug for k in kw):
            continue
        out.append({"title": slug.replace("-", " ").capitalize(), "url": "https://www.kitco.com" + path,
                    "provider": "Kitco News", "published": f"{d}T00:00:00+00:00"})
    return out[:limit]


def article_text(url: str) -> list[str]:
    """抓取正文段落；付费墙/失败返回空列表。"""
    if any(d in url for d in PAYWALLED):
        return []
    try:
        html = http_get(url, timeout=15, retries=1).text
    except Exception as e:  # noqa: BLE001
        log.info("article fetch failed %s: %s", url, e)
        return []
    soup = BeautifulSoup(html, "lxml")
    ps = soup.select("div.body p, div.caas-body p, article p") or soup.select("p")
    return [p.get_text(" ", strip=True) for p in ps if len(p.get_text(strip=True)) > 40]


# ---- SEC EDGAR ----
ITEM_CN = {
    "1.01": "签订重大协议", "1.02": "终止重大协议", "1.03": "破产/接管", "2.01": "完成收购或处置资产",
    "2.02": "经营业绩公告", "2.03": "新增重大债务", "2.05": "重组/退出成本", "2.06": "重大减值",
    "3.01": "退市或不符合上市标准通知", "3.02": "未注册股权发行", "4.01": "更换审计师",
    "4.02": "既往财报不可依赖", "5.01": "控制权变更", "5.02": "董事/高管变动", "7.01": "Reg FD 披露",
    "8.01": "其他重大事件", "9.01": "财务报表与附件",
}


def _cik_map(ua: str) -> dict[str, int]:
    path = CACHE / "sec_tickers.json"
    c = read_json(path)
    if c and (utcnow().timestamp() - c["ts"]) < 7 * 86400:
        return c["map"]
    j = http_get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": ua}).json()
    m = {v["ticker"].replace(".", "-"): v["cik_str"] for v in j.values()}
    write_json(path, {"ts": utcnow().timestamp(), "map": m})
    return m


def sec_filings(ticker: str, session: date, ua: str) -> list[dict]:
    """交易日当天及前一自然日提交的 8-K/6-K/SC 13D/S-1/等公告。"""
    if not ua:
        return []
    cik = _cik_map(ua).get(ticker)
    if not cik:
        return []
    j = http_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers={"User-Agent": ua}).json()
    r = j["filings"]["recent"]
    days = {session.isoformat(), (session - timedelta(days=1)).isoformat()}
    keep = ("8-K", "6-K", "SC 13D", "SC TO", "S-4", "DEFM14A", "10-Q", "10-K")
    out, seen = [], set()
    for i, form in enumerate(r["form"]):
        if r["filingDate"][i] not in days or not form.startswith(keep):
            continue
        acc = r["accessionNumber"][i].replace("-", "")
        items = [x.strip() for x in (r.get("items", [""] * len(r["form"]))[i] or "").split(",") if x.strip()]
        key = (form, r["filingDate"][i], tuple(items))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "form": form, "filed": r["filingDate"][i],
            "item_desc": [f"{x} {ITEM_CN.get(x, '')}".strip() for x in items if x != "9.01"],
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{r['primaryDocument'][i]}",
        })
    return out
