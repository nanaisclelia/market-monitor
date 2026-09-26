"""入口。

  python -m src.run --auto                 # 由 launchd 每 10 分钟调用：到点且未完成的市场才执行
  python -m src.run --market us            # 手动：最近一个已收盘交易日
  python -m src.run --market us --date 2026-09-24 --force
  python -m src.run --render               # 仅用已有快照重新生成页面
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from . import build_uk, build_us, calendars, render
from .common import DATA, SNAPSHOTS, get_logger, read_json, settings, write_json

log = get_logger()
BUILDERS = {"us": build_us, "uk": build_uk}
STATE = DATA / "state.json"


def run_market(market: str, session: date) -> bool:
    log.info("=== %s session %s ===", market, session)
    mod = BUILDERS[market]
    snap = mod.build(session)
    write_json(SNAPSHOTS / session.isoformat() / f"{market}.json", snap)
    render.render_all()
    ok = mod.core_ok(snap)
    log.info("%s %s done: core_ok=%s errors=%d", market, session, ok, len(snap["errors"]))
    return ok


def auto() -> None:
    cfg = settings()
    state = read_json(STATE, {})
    for market in BUILDERS:
        m = cfg["markets"][market]
        session = calendars.due_session(m["calendar"], m["delay_minutes"])
        if session is None:
            continue
        s = state.get(market, {})
        if s.get("session") == session.isoformat() and (s.get("done") or s.get("attempts", 0) >= cfg["max_auto_attempts"]):
            continue
        attempts = s.get("attempts", 0) + 1 if s.get("session") == session.isoformat() else 1
        try:
            ok = run_market(market, session)
        except Exception:  # noqa: BLE001
            log.exception("%s run failed", market)
            ok = False
        state[market] = {"session": session.isoformat(), "attempts": attempts, "done": ok}
        write_json(STATE, state)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--auto", action="store_true")
    p.add_argument("--market", choices=list(BUILDERS))
    p.add_argument("--date", type=date.fromisoformat)
    p.add_argument("--force", action="store_true", help="跳过「是否交易日/是否已收盘」检查")
    p.add_argument("--render", action="store_true")
    a = p.parse_args(argv)
    if a.render:
        render.render_all()
        return 0
    if a.auto:
        auto()
        return 0
    if not a.market:
        p.error("需要 --auto、--market 或 --render")
    m = settings()["markets"][a.market]
    session = a.date or calendars.latest_session(m["calendar"])
    if not a.force and not calendars.is_session(m["calendar"], session):
        log.error("%s 不是 %s 交易日；如需强制运行加 --force", session, m["calendar"])
        return 1
    return 0 if run_market(a.market, session) else 2


if __name__ == "__main__":
    sys.exit(main())
