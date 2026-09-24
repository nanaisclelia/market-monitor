"""具名专家观点提取（纯规则，无 LLM）。

只收录同时满足以下条件的句子：
  1. 句中出现人名，且人名与白名单机构以「X, analyst at Firm」「Firm analyst X」「X of Firm」等结构相连；
  2. 句中有归属动词或引号（said / wrote / noted / expects / “…”）；
  3. 句子与标的相关（含标的关键词）。
句子逐字引用原文，附发布方、日期、链接。找不到时由调用方显示「暂无可核实的专家观点」。
"""
from __future__ import annotations

import re

from .common import load_yaml
from .fetchers import news

NAME = r"(?P<name>[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?(?:\s[A-Z]\.)?\s(?:(?:van|von|de|du|da|le)\s)?[A-Z][a-zA-Z'\-]+)"
ROLE = (r"(?i:analysts?|strategists?|economists?|head|chief|director|manager|partner|trader|"
        r"CIO|CEO|president|researcher|specialist|portfolio manager|fund manager|founder)")
ATTRIB = re.compile(r"\b(said|says|say|wrote|writes|noted|notes|told|added|expects?|sees|according to|"
                    r"forecasts?|warned|argued|believes?|reiterated|upgraded|downgraded|raised|cut|rates?|rating|rated|"
                    r"assumed|initiated|maintained|lowered|price target)\b|[\"“”]")
NOT_NAMES = {"Wall Street", "New York", "Federal Reserve", "United States", "Yahoo Finance", "Treasury Secretary"}
# 人名中不应出现的职位/机构词（防止把 "Market Analyst" 当成人名）
TITLE_WORDS = {"analyst", "analysts", "strategist", "strategists", "senior", "chief", "head", "market", "markets",
               "commodity", "commodities", "director", "manager", "economist", "research", "global", "investment",
               "officer", "vice", "president", "managing", "partner", "portfolio", "trader", "metals", "precious",
               "executive", "editor", "reporter", "bank", "capital", "securities", "group", "fund", "equity", "the"}


def _is_name(n: str) -> bool:
    return n not in NOT_NAMES and not ({w.lower().strip(".'") for w in n.split()} & TITLE_WORDS)


def _patterns(firms: list[str]):
    F = "(?P<firm>" + "|".join(re.escape(f) for f in sorted(firms, key=len, reverse=True)) + ")"
    return [
        re.compile(NAME + r",\s+(?:an?\s+|the\s+)?[^,.;]{0,80}?\b" + ROLE + r"\b[^,.;]{0,60}?\s(?:at|of|with|for)\s+" + F),
        re.compile(F + r"(?:'s|’s)?\s+(?:[A-Za-z\-]+\s){0,3}?" + ROLE + r"\s+(?:led by\s+|including\s+)?" + NAME),
        re.compile(NAME + r"\s+(?:of|at|from)\s+" + F + r"\b"),
    ]


_SPLIT = re.compile(r"(?<=[.!?”\"])\s+(?=[A-Z“\"])")


def extract(sentences: list[str], keywords: list[str], firms: list[str]) -> list[dict]:
    pats = _patterns(firms)
    kw = re.compile("|".join(re.escape(k) for k in keywords), re.I) if keywords else None
    out = []
    for s in sentences:
        if not ATTRIB.search(s):
            continue
        m = next((m for p in pats for m in p.finditer(s) if _is_name(m.group("name"))), None)
        if m:
            out.append({"who": m.group("name"), "org": m.group("firm"), "quote": s})
    return [o for o in out if not kw or kw.search(o["quote"])]


def for_target(ticker: str, keywords: list[str], max_articles: int = 6, max_quotes: int = 3,
               extra: list[dict] | None = None) -> dict:
    firms = load_yaml("insights.yaml").get("institutions", [])
    stories = (extra or []) + news.recent_stories(ticker)
    found, seen = [], set()
    scanned = 0
    for st in stories:
        if scanned >= max_articles or len(found) >= max_quotes:
            break
        paras = news.article_text(st["url"])
        if not paras:
            continue
        scanned += 1
        sents = [x for p in paras for x in _SPLIT.split(p)]
        for q in extract(sents, keywords, firms):
            key = (q["who"], q["quote"][:80])
            if key in seen:
                continue
            seen.add(key)
            found.append({**q, "publisher": st["provider"], "date": st["published"][:10],
                          "url": st["url"], "title": st["title"]})
            if len(found) >= max_quotes:
                break
    return {"insights": found, "articles_scanned": scanned,
            "headlines": [{k: s[k] for k in ("title", "url", "provider", "published")} for s in stories[:5]]}
