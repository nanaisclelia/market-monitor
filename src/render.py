"""快照 → 静态 HTML（site/index.html + site/archive/<日期>.html）。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .common import DATA, ROOT, SITE, SNAPSHOTS, read_json, settings, utcnow

MARKETS = ["cn", "uk", "us"]


def _fmt_time(iso_s: str | None, tz: str, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    if not iso_s:
        return "—"
    return datetime.fromisoformat(iso_s).astimezone(ZoneInfo(tz)).strftime(fmt)


def _num(v, d=2, sign=False, pct=False):
    if v is None:
        return "—"
    s = f"{v:+,.{d}f}" if sign else f"{v:,.{d}f}"
    return s + ("%" if pct else "")


def _big(v):
    if v is None:
        return "—"
    for n, u in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= n:
            return f"{v / n:.2f}{u}"
    return f"{v:.0f}"


def _spark(vals, w: int = 96, h: int = 26, label: str = "") -> Markup:
    """走势迷你图（内联 SVG，颜色随涨跌取主题 token）。"""
    vals = [v for v in (vals or []) if v is not None]
    if len(vals) < 2:
        return Markup("")
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pad = 3
    xs = [pad + i * (w - 2 * pad) / (len(vals) - 1) for i in range(len(vals))]
    ys = [pad + (hi - v) * (h - 2 * pad) / rng for v in vals]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    cls = "up" if vals[-1] >= vals[0] else "dn"
    area = f"{xs[0]:.1f},{h} " + pts + f" {xs[-1]:.1f},{h}"
    chg = (vals[-1] / vals[0] - 1) * 100
    title = f"{label} 近 {len(vals)} 个交易日 {chg:+.1f}%"
    return Markup(
        f'<svg class="spark {cls}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{title}">'
        f"<title>{title}</title>"
        f'<polygon points="{area}" fill="currentColor" opacity=".10"/>'
        f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="2.2" fill="currentColor"/></svg>'
    )


_EN_ITEMS = {
    "1.01": "Material agreement", "1.02": "Termination of agreement", "1.03": "Bankruptcy", "2.01": "Acquisition/disposition completed",
    "2.02": "Results of operations", "2.03": "Direct financial obligation", "2.05": "Exit/restructuring costs", "2.06": "Material impairment",
    "3.01": "Delisting notice", "3.02": "Unregistered equity sale", "4.01": "Auditor change", "4.02": "Non-reliance on financials",
    "5.01": "Change in control", "5.02": "Director/officer change", "7.01": "Reg FD disclosure", "8.01": "Other events",
}
_EN_PHRASES = [
    ("|涨跌幅| ≥ ", "|Move| ≥ "), ("（20日σ=", " (20d σ="), ("（", " ("), ("）", ")"), ("、", ", "), ("；", "; "),
    ("期货", "futures"), ("涨停", "Limit-up"), ("跌停", "Limit-down"), ("上调评级", "Upgrade"), ("下调评级", "Downgrade"),
    ("首次覆盖", "Initiate"), ("无数据", "no data"), ("最新 K 线", "latest bar"), ("重申", "Reiterate"), ("维持", "Maintain"), ("主题：", "Theme: "),
]


def _roles_en() -> dict:
    from .common import load_yaml
    return {m["role"]: m.get("role_en", m["role"]) for t in load_yaml("themes.yaml").get("themes", []) for m in t["members"]}


def _en(v) -> str:
    """程序生成的中文短语 → 英文（用于中英切换）。"""
    if v is None:
        return ""
    s = str(v)
    code = s.split(" ")[0]
    if code in _EN_ITEMS and s.startswith(code + " "):
        return f"{code} {_EN_ITEMS[code]}"
    for zh, en in sorted(_roles_en().items(), key=lambda x: -len(x[0])):
        s = s.replace(zh, en)
    for zh, en in _EN_PHRASES:
        s = s.replace(zh, en)
    return s


# ---- 可信度分级 ----
# A  当日一手披露（公司公告 / SEC / 交易所 / 官方文件，且发布于当日）
# Ab 背景一手材料（一手来源，但并非当日发布，不能作为当日催化剂）
# B+ 两家以上独立来源一致（至少一家主流）
# B  主流媒体单源
# C  低置信单源 / 间接报道（内容农场、自动生成稿、博客式分析站）
# D  AI 推断，待验证
_PRIMARY_MARKS = ("SEC", "EDGAR", "新闻稿", "press release", "交易所", "Exchange", "federalreserve", "Federal Reserve", "RNS")
_MAINSTREAM = ("reuters", "bloomberg", "financial times", "wall street journal", "wsj", "cnbc", "barron", "associated press",
               "mt newswires", "investing.com", "investor's business daily", "the national", "american banker",
               "supply chain dive", "benzinga", "yahoo finance", "business today", "rttnews", "hotel dive", "healthcare dive")
GRADE_RANK = {"A": 6, "B+": 5, "Ab": 4, "B": 3, "C": 2, "D": 1}


def _is_primary(src: dict) -> bool:
    txt = f"{src.get('publisher', '')} {src.get('publisher_en', '')} {src.get('url', '')}"
    return any(m.lower() in txt.lower() for m in _PRIMARY_MARKS)


def _root(src: dict) -> str:
    """出版方归一：「MT Newswires / Yahoo Finance」算 MT Newswires；「Business Today（援引 Bloomberg）」算 Bloomberg。"""
    pub = (src.get("publisher_en") or src.get("publisher") or "").lower()
    if "citing bloomberg" in pub or "援引 bloomberg" in pub:
        return "bloomberg"
    return pub.split("/")[0].split("(")[0].split("（")[0].strip()


def _is_mainstream(src: dict) -> bool:
    return _is_primary(src) or any(m in _root(src) or m in (src.get("publisher") or "").lower() for m in _MAINSTREAM)


def _cited(item: dict, sources: list) -> list:
    return [sources[i] for i in item.get("src", []) if i < len(sources)] if "src" in item else list(sources)


def _grade(item: dict, sources: list, session: str | None = None) -> str:
    """单条要点 / 事件的证据强度；分析数据中显式给出的 grade 优先。"""
    if item.get("grade"):
        return item["grade"]
    cited = _cited(item, sources)
    if item.get("tag") == "AI 推断" or not cited:
        return "D"
    prim = [x for x in cited if _is_primary(x)]
    if item.get("tag") == "已证实" and prim:
        return "A" if session and any((x.get("date") or "")[:10] == session for x in prim) else "Ab"
    roots = {_root(x) for x in cited}
    if len(roots) >= 2 and any(_is_mainstream(x) for x in cited):
        return "B+"
    if any(_is_mainstream(x) for x in cited):
        return "B"
    return "C"


def _evidence(w: dict | None, session: str | None = None) -> dict:
    """证据数量 / 最新来源日期 / 一手材料是否为当日。"""
    srcs = (w or {}).get("sources", [])
    dates = sorted(x.get("date", "") for x in srcs if x.get("date"))
    prim = [x for x in srcs if _is_primary(x)]
    today = any(session and (x.get("date") or "")[:10] == session for x in prim)
    return {"n": len(srcs), "latest": dates[-1] if dates else None,
            "primary": "today" if today else ("background" if prim else "none")}


def _best_grade(w: dict | None, session: str | None = None) -> str:
    if not w:
        return "D"
    gs = [_grade(p, w.get("sources", []), session) for p in w.get("points", []) if not p.get("not_catalyst")]
    return max(gs, key=lambda g: GRADE_RANK[g]) if gs else "D"


def _open_ticker(hits: list, stocks: dict, session: str | None) -> str | None:
    """每个市场默认只展开 1 张卡：证据最强者，其次涨跌幅最大者。"""
    best, key = None, None
    for h in hits:
        w = stocks.get(h["ticker"])
        if not (h.get("detail") or w):
            continue
        k = (GRADE_RANK[_best_grade(w, session)] if w else 0, abs(h["stats"]["pct"]))
        if key is None or k > key:
            best, key = h["ticker"], k
    return best


def _hot(v, thr) -> str:
    """极端值徽标：|v| ≥ 阈值时给单元格加色块。"""
    if v is None:
        return ""
    return "hotup" if v >= thr else ("hotdn" if v <= -thr else "")


def _env():
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
    env.filters.update(num=_num, big=_big, t=_fmt_time, spark=_spark, en=_en)
    env.globals.update(grade=_grade, evidence=_evidence, best_grade=_best_grade, hot=_hot, open_ticker=_open_ticker, GRADE_RANK=GRADE_RANK)
    return env


def _dates():
    return sorted((p.name for p in SNAPSHOTS.iterdir() if p.is_dir()), reverse=True) if SNAPSHOTS.exists() else []


def _page(env, snaps: dict, title_date: str, archive: list[str], is_archive: bool, public: bool = False) -> str:
    cfg = settings()
    return env.get_template("dashboard.html.j2").render(
        s=snaps, date=title_date, archive=archive, is_archive=is_archive, public=public,
        disp_tz=cfg["display_tz"], now=utcnow().isoformat(), tol=cfg["cross_check_tolerance_pct"],
    )


def _with_analysis(snap: dict | None) -> dict | None:
    """附上 AI 整理的原因分析（data/analysis/<交易日>.json，由 Claude 定时任务写入）。"""
    if snap:
        snap = {**snap, "analysis": read_json(DATA / "analysis" / f"{snap['session']}.json")}
    return snap


def render_all() -> None:
    env = _env()
    dates = _dates()
    (SITE / "archive").mkdir(parents=True, exist_ok=True)
    # 首页：每个市场各取最新快照
    latest = {}
    for m in MARKETS:
        for d in dates:
            j = read_json(SNAPSHOTS / d / f"{m}.json")
            if j:
                latest[m] = _with_analysis(j)
                break
    (SITE / "index.html").write_text(_page(env, latest, dates[0] if dates else "—", dates, False), encoding="utf-8")
    # 公开版：不含新闻原句与存档链接，用于发布到网页
    (SITE / "public.html").write_text(_page(env, latest, dates[0] if dates else "—", [], False, public=True), encoding="utf-8")
    for d in dates:
        snaps = {m: _with_analysis(j) for m in MARKETS if (j := read_json(SNAPSHOTS / d / f"{m}.json"))}
        (SITE / "archive" / f"{d}.html").write_text(_page(env, snaps, d, dates, True), encoding="utf-8")
