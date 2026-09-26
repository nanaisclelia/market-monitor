"""CNBC 行情接口：作为第二数据源做交叉校验。"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from ..common import http_get

URL = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
       "?symbols={s}&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=0&output=json")


def _num(x):
    try:
        return float(str(x).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def quotes(symbols: list[str], chunk: int = 40) -> dict[str, dict]:
    """返回 {symbol: {last, prev_close, change_pct, time}}。"""
    out = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        r = http_get(URL.format(s=quote("|".join(batch), safe="")))
        rows = r.json()["FormattedQuoteResult"]["FormattedQuote"]
        if isinstance(rows, dict):
            rows = [rows]
        for q in rows:
            if str(q.get("code")) != "0" or _num(q.get("last")) is None:
                continue
            t = q.get("last_time")
            out[q["symbol"]] = {
                "last": _num(q.get("last")),
                "prev_close": _num(q.get("previous_day_closing")),
                "change_pct": _num(q.get("change_pct")),
                "time": datetime.fromisoformat(t).isoformat() if t else None,
                "source": "CNBC quote",
            }
    return out


def yahoo_to_cnbc(ticker: str) -> str:
    """美股个股代码转换（BRK-B -> BRK.B）。"""
    return ticker.replace("-", ".")


def uk_symbol(epic: str) -> str:
    """LSE 代码 → CNBC 代码（BARC → BARC-GB）。"""
    return f"{epic}-GB"
