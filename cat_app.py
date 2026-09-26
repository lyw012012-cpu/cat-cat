#!/usr/bin/env python3
"""
집사 고양이 v0.1 — 스파이크의 감지·규칙 로직 + SQLite 저장

스파이크(cat-spike/watch.py)에서 검증한 것은 그대로 가져다 쓰고,
이번에 더한 것은 "규칙을 DB에서 읽고, 기록을 DB에 쓰는 것" 하나다.

실행
    python cat_app.py --simulate        # 아무 OS에서나. 메모리 DB로 로직 확인
    python cat_app.py                   # Windows. 실제 감지 → cat.db 에 저장
    python cat_app.py --action close    # 규칙이 close면 실제로 창을 닫는다 (주의!)
    python cat_app.py --report          # 오늘 가장 많이 쓴 앱 TOP 5
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

from watch import (
    DEFAULT_RULES, Condition, Rule, Session, SessionTracker,
    SimulatedProbe, WindowsProbe, _host, now_iso, pick_rule,
)

IDLE_THRESHOLD_SEC = 60

# 사용 기록은 개인정보라 코드 폴더(클라우드 동기화·git) 밖, 이 PC 로컬에만 둔다.
DEFAULT_DB = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "cat-app", "cat.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS block_rule (
    rule_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    action    TEXT NOT NULL CHECK (action IN ('close', 'warn', 'delay', 'mute')),
    priority  INTEGER NOT NULL DEFAULT 100,      -- 낮을수록 먼저
    reaction  TEXT NOT NULL DEFAULT '',          -- 고양이 대사
    enabled   INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);

-- 같은 group_no 끼리는 OR, 서로 다른 group_no 사이는 AND
CREATE TABLE IF NOT EXISTS rule_condition (
    condition_id INTEGER PRIMARY KEY,
    rule_id      TEXT NOT NULL REFERENCES block_rule(rule_id) ON DELETE CASCADE,
    group_no     INTEGER NOT NULL DEFAULT 0,
    subject      TEXT NOT NULL CHECK (subject IN ('app', 'url', 'window_title')),
    operator     TEXT NOT NULL CHECK (operator IN ('eq', 'contains', 'regex')),
    value        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_session (
    session_id   INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,                  -- UTC ISO-8601 (…Z)
    ended_at     TEXT,
    exe          TEXT NOT NULL,
    window_title TEXT,                           -- 민감 정보: 로컬 전용
    url_host     TEXT,                           -- 전체 URL 대신 호스트만
    duration_sec REAL NOT NULL,
    is_idle      INTEGER NOT NULL CHECK (is_idle IN (0, 1))
);
CREATE INDEX IF NOT EXISTS ix_session_started ON usage_session(started_at);

CREATE TABLE IF NOT EXISTS block_event (
    event_id    INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    rule_id     TEXT REFERENCES block_rule(rule_id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    exe         TEXT,
    url_host    TEXT
);
"""


# =============================================================================
#  DB
# =============================================================================

def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    if db.execute("SELECT COUNT(*) FROM block_rule").fetchone()[0] == 0:
        save_rules(db, DEFAULT_RULES)                # 첫 실행: 기본 규칙 심기
    return db


def save_rules(db: sqlite3.Connection, rules) -> None:
    with db:
        for r in rules:
            db.execute("INSERT INTO block_rule (rule_id, name, action, priority, reaction)"
                       " VALUES (?, ?, ?, ?, ?)",
                       (r.rule_id, r.name, r.action, r.priority, r.reaction))
            db.executemany("INSERT INTO rule_condition (rule_id, group_no, subject, operator, value)"
                           " VALUES (?, ?, ?, ?, ?)",
                           [(r.rule_id, c.group_no, c.subject, c.operator, c.value)
                            for c in r.conditions])


def load_rules(db: sqlite3.Connection) -> list[Rule]:
    conds: dict[str, list[Condition]] = {}
    for rule_id, g, s, o, v in db.execute(
            "SELECT rule_id, group_no, subject, operator, value FROM rule_condition"
            " ORDER BY condition_id"):
        conds.setdefault(rule_id, []).append(Condition(g, s, o, v))
    return [Rule(rid, name, action, prio, tuple(conds.get(rid, ())), reaction)
            for rid, name, action, prio, reaction in db.execute(
                "SELECT rule_id, name, action, priority, reaction FROM block_rule"
                " WHERE enabled = 1")]


def save_session(db: sqlite3.Connection, s: Session) -> None:
    with db:
        db.execute("INSERT INTO usage_session (started_at, ended_at, exe, window_title,"
                   " url_host, duration_sec, is_idle) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (s.started_at, s.ended_at, s.exe, s.title, _host(s.url),
                    round(s.duration_sec, 1), int(s.is_idle)))


def save_event(db: sqlite3.Connection, rule: Rule, win) -> None:
    with db:
        db.execute("INSERT INTO block_event (occurred_at, rule_id, action, exe, url_host)"
                   " VALUES (?, ?, ?, ?, ?)",
                   (now_iso(), rule.rule_id, rule.action, win.exe, _host(win.url)))


def top_apps_today(db: sqlite3.Connection, limit: int = 5) -> list[tuple[str, float]]:
    """설계서 Q2: 오늘(현지 날짜) 가장 많이 쓴 앱. 자리 비움은 뺀다."""
    return db.execute(
        "SELECT exe, SUM(duration_sec) AS total FROM usage_session"
        " WHERE is_idle = 0 AND date(started_at, 'localtime') = date('now', 'localtime')"
        " GROUP BY exe ORDER BY total DESC LIMIT ?", (limit,)).fetchall()


def print_report(db: sqlite3.Connection) -> None:
    rows = top_apps_today(db)
    print("\n" + "─" * 46 + "\n오늘 가장 많이 쓴 앱 TOP 5\n" + "─" * 46)
    for exe, sec in rows:
        print(f"  {exe:<24} {sec / 60:>6.1f}분")
    if not rows:
        print("  (오늘 기록 없음)")
    blocked = db.execute("SELECT COUNT(*) FROM block_event"
                         " WHERE date(occurred_at, 'localtime') = date('now', 'localtime')"
                         ).fetchone()[0]
    print("─" * 46 + f"\n고양이가 오늘 막은 횟수: {blocked}")


# =============================================================================
#  메인 루프
# =============================================================================

def run(db: sqlite3.Connection, probe, interval: float, action: str, stop_when_empty: bool) -> None:
    rules = load_rules(db)
    tracker = SessionTracker(lambda s: save_session(db, s))
    try:
        while True:
            win = probe.probe()
            if win is None and stop_when_empty:
                break
            idle = probe.idle_seconds() > IDLE_THRESHOLD_SEC
            if tracker.observe(win, interval, idle) and win is not None:
                print(f"[{now_iso()}] {win.exe:<14} {win.title[:50]}")
                rule = pick_rule(rules, win)             # 새 창마다 한 번만 판정
                if rule:
                    print(f"  🐱 [{rule.name}] \"{rule.reaction}\"")
                    save_event(db, rule, win)
                    if action == "close" and rule.action == "close":
                        probe.close_window(win.hwnd)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n중단됨.")
    finally:
        tracker.flush()


def main() -> int:
    # 한국어 Windows 콘솔(cp949)에서 이모지·특수문자 출력으로 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="집사 고양이 v0.1")
    ap.add_argument("--simulate", action="store_true", help="가짜 시나리오 + 메모리 DB")
    ap.add_argument("--report", action="store_true", help="오늘 사용 통계만 출력")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"기본값: {DEFAULT_DB}")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--action", choices=["log", "close"], default="log")
    args = ap.parse_args()

    if args.simulate:
        db = connect(":memory:")
        run(db, SimulatedProbe(), 0.05, "log", stop_when_empty=True)
    elif args.report:
        db = connect(args.db)
    elif sys.platform != "win32":
        print("실제 감지는 Windows 전용입니다. 로직 확인: python cat_app.py --simulate")
        return 1
    else:
        db = connect(args.db)
        print(f"=== 감시 시작 (DB: {args.db}) — Ctrl+C 로 종료 ===")
        run(db, WindowsProbe(), args.interval, args.action, stop_when_empty=False)

    print_report(db)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
