"""LBMA 贵金属定价（伦敦基准价，USD）。"""
from __future__ import annotations

from ..common import http_get

URL = "https://prices.lbma.org.uk/json/{key}.json"
# 定价公布时间（伦敦）：金 PM 15:00，银 12:00，铂/钯 PM 14:00
FIX_TIME = {"gold_pm": "15:00", "silver": "12:00", "platinum_pm": "14:00", "palladium_pm": "14:00"}


def fixes(key: str, n: int = 30) -> list[tuple[str, float]]:
    """最近 n 个定价 [(date, usd)]，已剔除空值。"""
    rows = http_get(URL.format(key=key)).json()
    out = [(r["d"], float(r["v"][0])) for r in rows if r.get("v") and r["v"][0]]
    return out[-n:]
