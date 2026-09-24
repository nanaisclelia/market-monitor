"""监控标的池：S&P 500（Wikipedia）+ Nasdaq-100（Nasdaq 官方 API），本地缓存 7 天。"""
from __future__ import annotations

import io
import time

import pandas as pd

from ..common import CACHE, get_logger, http_get, read_json, write_json

log = get_logger()
TTL = 7 * 86400


def _cached(name: str, loader):
    path = CACHE / f"universe_{name}.json"
    c = read_json(path)
    if c and time.time() - c["ts"] < TTL:
        return c["items"]
    try:
        items = loader()
        write_json(path, {"ts": time.time(), "items": items})
        return items
    except Exception as e:  # noqa: BLE001
        if c:
            log.warning("universe %s refresh failed (%s); using cache", name, e)
            return c["items"]
        raise


def _sp500():
    html = http_get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies").text
    df = pd.read_html(io.StringIO(html))[0]
    return [{"ticker": s.replace(".", "-"), "name": n, "sector": g}
            for s, n, g in zip(df["Symbol"], df["Security"], df["GICS Sector"])]


def _nasdaq100():
    j = http_get("https://api.nasdaq.com/api/quote/list-type/nasdaq100").json()
    return [{"ticker": r["symbol"].replace(".", "-").replace("/", "-"), "name": r["companyName"], "sector": None}
            for r in j["data"]["data"]["rows"]]


LOADERS = {"sp500": _sp500, "nasdaq100": _nasdaq100}


def us(sets: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in sets:
        for it in _cached(s, LOADERS[s]):
            out.setdefault(it["ticker"], it)
    return out
