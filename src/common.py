"""共享工具：路径、配置、日志、HTTP、带来源的数据记录。"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots"
CACHE = DATA / "cache"
SITE = ROOT / "site"
LOGS = ROOT / "logs"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


def load_yaml(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8")) or {}


def settings() -> dict:
    """settings.yaml 为公开默认值；settings.local.yaml（不提交到 git）覆盖其中的个人配置，如联系邮箱。"""
    cfg = load_yaml("settings.yaml")
    if (CONFIG / "settings.local.yaml").exists():
        cfg.update(load_yaml("settings.local.yaml"))
    return cfg


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def get_logger() -> logging.Logger:
    log = logging.getLogger("findash")
    if not log.handlers:
        LOGS.mkdir(exist_ok=True)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler(LOGS / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)
        log.setLevel(logging.INFO)
    return log


def http_get(url: str, *, headers: dict | None = None, timeout: int = 20,
             retries: int = 3, backoff: float = 2.0) -> requests.Response:
    h = {"User-Agent": UA}
    h.update(headers or {})
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=h, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(backoff * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


class ErrorLog:
    """收集本次运行的抓取错误：写日志，同时写入快照在页面展示。"""

    def __init__(self):
        self.items: list[dict] = []
        self.log = get_logger()

    def add(self, section: str, msg: str):
        self.log.error("[%s] %s", section, msg)
        self.items.append({"section": section, "error": msg, "at": iso(utcnow())})


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
