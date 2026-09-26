#!/usr/bin/env python3
"""
집사 고양이 v0.1 — 스파이크의 감지·규칙 로직 + SQLite 저장

스파이크(cat-spike/watch.py)에서 검증한 것은 그대로 가져다 쓰고,
이번에 더한 것은 "규칙을 DB에서 읽고, 기록을 DB에 쓰는 것" 하나다.

실행
    python cat_app.py --simulate        # 아무 OS에서나. 메모리 DB로 로직 확인
    python cat_app.py                   # Windows. 고양이 창이 뜨고 감시 모드로 시작
    python cat_app.py 업무모드           # 업무모드(쇼츠·릴스 탭 차단)로 시작
    모드는 켜져 있는 동안 고양이 창에서 언제든 바꾼다. 창을 닫으면 종료.
    이미 켜져 있으면 두 번째 실행은 거부된다.
    python cat_app.py --report          # 오늘 가장 많이 쓴 앱 TOP 5
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
import threading

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

def block(probe, win) -> bool:
    """브라우저는 그 탭만(Ctrl+W), 다른 앱은 창을 닫는다."""
    if win.exe.lower() in WindowsProbe.BROWSERS:
        return probe.close_tab(win.hwnd)
    return probe.close_window(win.hwnd)


_instance_lock = None


def single_instance(name: str = "Local\\jipsa-cat") -> bool:
    """
    이미 켜져 있으면 False. Windows '이름 있는 뮤텍스'를 잡아 둔다 — 프로세스가
    어떻게 끝나든(강제 종료 포함) OS가 풀어주므로 잠금 파일처럼 남는 일이 없다.
    """
    global _instance_lock
    if sys.platform != "win32":
        return True
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == 183:              # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _instance_lock = handle                          # 프로세스가 끝날 때까지 쥐고 있음
    return True


class Control:
    """제어 창(메인 스레드)과 감시 루프(작업 스레드)가 함께 보는 상태."""

    def __init__(self, action: str) -> None:
        self.action = action                 # 'log' | 'close' — 창에서 바꾸면 루프가 다음 판정부터 따른다
        self.last = "감시 시작"               # 창에 보여줄 최근 소식
        self.stop = threading.Event()


MODES = (("log", "👀  감시 모드", "기록만 하고, 쇼츠는 볼 수 있어요"),
         ("close", "💼  업무모드", "쇼츠·릴스 탭을 닫아요"))


def mode_name(action: str) -> str:
    return next(name for a, name, _ in MODES if a == action).strip("👀💼 ")


def control_window(ctl: Control) -> None:
    """앱이 켜져 있는 동안 떠 있는 창. 모드를 언제든 바꿀 수 있고, 닫으면 앱이 끝난다."""
    import signal
    import tkinter as tk

    root = tk.Tk()
    root.title("집사 고양이")
    root.resizable(False, False)
    tk.Label(root, text="🐱 집사 고양이", font=("맑은 고딕", 12, "bold"), pady=10).pack()

    mode = tk.StringVar(value=ctl.action)

    def switch() -> None:
        ctl.action = mode.get()
        ctl.last = f"모드 변경 → {mode_name(ctl.action)}"
        print(f"=== {ctl.last} ===")

    for action, name, desc in MODES:
        tk.Radiobutton(root, text=f"{name}\n{desc}", variable=mode, value=action,
                       command=switch, indicatoron=False, selectcolor="#ffe8a3",
                       width=30, pady=8, font=("맑은 고딕", 10)).pack(padx=20, pady=3)

    status = tk.Label(root, fg="#555", wraplength=260, pady=10, font=("맑은 고딕", 9))
    status.pack()
    tk.Label(root, text="창을 닫으면 고양이도 쉽니다", fg="#999", font=("맑은 고딕", 8)).pack(pady=(0, 8))

    def tick() -> None:                      # 작업 스레드 소식을 0.5초마다 창에 반영
        status.config(text=ctl.last)
        if ctl.stop.is_set():                # 감시 루프가 오류로 멈춘 경우
            root.destroy()
            return
        root.after(500, tick)

    # 터미널 Ctrl+C로도 끌 수 있게 (Tk 대기 중에는 KeyboardInterrupt가 전달되지 않음)
    signal.signal(signal.SIGINT, lambda *_: root.after(0, root.destroy))
    tick()
    root.lift()
    root.focus_force()
    root.mainloop()


def run(db: sqlite3.Connection, probe, interval: float, ctl: Control, stop_when_empty: bool) -> None:
    rules = load_rules(db)
    tracker = SessionTracker(lambda s: save_session(db, s))
    try:
        while not ctl.stop.is_set():
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
                    ctl.last = f"🐱 {rule.name} — 기록만 함"
                    if ctl.action == "close" and rule.action == "close":
                        ok = block(probe, win)
                        ctl.last = f"🐱 {rule.name} — {'탭을 닫음' if ok else '창이 바뀌어서 닫지 않음'}"
                        print(f"  → {ctl.last}")
            ctl.stop.wait(interval)
    except KeyboardInterrupt:
        print("\n중단됨.")
    finally:
        tracker.flush()


def watch_in_background(db_path: str, interval: float, ctl: Control) -> None:
    """작업 스레드. SQLite 연결과 UI Automation은 쓰는 스레드 안에서 만들어야 한다."""
    try:
        import uiautomation
        uia_ready = uiautomation.UIAutomationInitializerInThread()
    except ImportError:
        uia_ready = contextlib.nullcontext()
    with uia_ready:
        db = connect(db_path)
        try:
            run(db, WindowsProbe(), interval, ctl, stop_when_empty=False)
        except Exception as e:                           # noqa: BLE001
            ctl.last = f"⚠ 오류로 멈춤: {type(e).__name__}: {e}"
            print(ctl.last)
            ctl.stop.set()
        finally:
            db.close()


def main() -> int:
    # 한국어 Windows 콘솔(cp949)에서 이모지·특수문자 출력으로 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="집사 고양이 v0.1")
    ap.add_argument("--simulate", action="store_true", help="가짜 시나리오 + 메모리 DB")
    ap.add_argument("--report", action="store_true", help="오늘 사용 통계만 출력")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"기본값: {DEFAULT_DB}")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--action", choices=["log", "close"], default="log",
                    help="처음 모드. log=감시(기본), close=막기. 창에서 언제든 바꿀 수 있다")
    ap.add_argument("mode", nargs="?", choices=["업무모드", "감시모드"],
                    help="처음 모드를 이름으로 지정 (= --action close / log)")
    args = ap.parse_args()
    if args.mode:
        args.action = "close" if args.mode == "업무모드" else "log"

    if args.simulate:
        db = connect(":memory:")
        run(db, SimulatedProbe(), 0.05, Control("log"), stop_when_empty=True)
    elif args.report:
        db = connect(args.db)
    elif sys.platform != "win32":
        print("실제 감지는 Windows 전용입니다. 로직 확인: python cat_app.py --simulate")
        return 1
    else:
        if not single_instance():
            print("🐱 집사 고양이가 이미 실행 중입니다. 떠 있는 고양이 창을 닫은 뒤 다시 실행하세요.")
            return 1
        ctl = Control(args.action)
        print(f"=== {mode_name(ctl.action)} 시작 (DB: {args.db}) — 고양이 창을 닫으면 종료 ===")
        worker = threading.Thread(target=watch_in_background,
                                  args=(args.db, args.interval, ctl), daemon=True)
        worker.start()
        control_window(ctl)                  # 창이 닫힐 때까지 여기서 대기
        ctl.stop.set()
        worker.join(timeout=5)               # 마지막 세션 저장을 기다린다
        db = connect(args.db)

    print_report(db)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
