"""判断账本：每日「待验证问题」发布后冻结，不可静默改写。

  data/calls/<交易日>.json   冻结文件：原文、依据、时限、置信度、反证条件，以及内容哈希
  复盘结论只允许三种：证据一致 / 证据不充分 / 被证伪（可附「待到期」表示时限未到）

冻结文件一旦存在就拒绝覆盖；改动只能以新文件的「更正」形式出现，git 历史提供第二层审计。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .common import DATA, read_json

CALLS = DATA / "calls"
VERDICTS = ("证据一致", "证据不充分", "被证伪", "待到期")


def digest(items: list[dict]) -> str:
    body = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def freeze(session: str, items: list[dict]) -> dict:
    """写入冻结文件；已存在则拒绝。"""
    CALLS.mkdir(parents=True, exist_ok=True)
    path = CALLS / f"{session}.json"
    if path.exists():
        raise FileExistsError(f"{path} 已冻结，不能覆盖；如需修正请发布更正")
    doc = {"session": session, "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "sha256": digest(items), "items": items}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def load(session: str) -> dict | None:
    doc = read_json(CALLS / f"{session}.json")
    if doc:
        doc["intact"] = digest(doc["items"]) == doc["sha256"]   # 文件被改动过则显示「校验失败」
    return doc
