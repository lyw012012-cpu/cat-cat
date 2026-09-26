#!/usr/bin/env python3
"""
집사 고양이 v0.1 — 스파이크의 감지·규칙 로직 + SQLite 저장

스파이크(cat-spike/watch.py)에서 검증한 것은 그대로 가져다 쓰고,
이번에 더한 것은 "규칙을 DB에서 읽고, 기록을 DB에 쓰는 것" 하나다.

실행
    python cat_app.py --simulate        # 아무 OS에서나. 메모리 DB로 로직 확인
    python cat_app.py                   # Windows. 고양이 창이 뜨고 감시 모드로 시작
    python cat_app.py 업무모드           # 업무모드(말하기 → 소리 끄기 → 기다리게 → 탭 닫기)로 시작
    모드는 켜져 있는 동안 고양이 창에서 언제든 바꾼다. 창을 닫으면 종료.
    이미 켜져 있으면 두 번째 실행은 거부된다.
    python cat_app.py --report          # 오늘 가장 많이 쓴 앱 TOP 5
"""

from __future__ import annotations

import argparse
import contextlib
import os
import queue
import re
import sqlite3
import sys
import threading
from datetime import date

from watch import (
    DEFAULT_RULES, Condition, Rule, Session, SessionTracker,
    STUDY_WORDS, SimulatedProbe, WindowInfo, WindowsProbe, _host, now_iso, pick_rule, rule_matches,
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
    window_title TEXT,                           -- 로컬 전용 (당근/채찍 판단용: 영상 제목 등)
    url_host     TEXT,
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

# 스키마 변경 이력. DB의 PRAGMA user_version 이 몇 번까지 적용됐는지 기억한다.
# 새 변경은 맨 뒤에 추가만 한다 — 이미 배포된 항목은 절대 고치지 않는다.
MIGRATIONS = [
    # v1: 웹 기록 강화 · 막은 기록의 실제 실행 여부 · 누적 시간 규칙 · 1초 이하 기록 정리
    """
    ALTER TABLE usage_session ADD COLUMN url TEXT;          -- 브라우저면 전체 주소 (쇼츠/강의 구분용)
    ALTER TABLE block_event   ADD COLUMN mode TEXT;         -- 그때 모드: 'log'(감시) | 'close'(업무), NULL=v0 기록
    ALTER TABLE block_event   ADD COLUMN executed INTEGER;  -- 실제로 닫았나: 1/0, NULL=v0 기록(알 수 없음)
    ALTER TABLE block_event   ADD COLUMN minutes INTEGER;   -- 누적 시간 규칙이면 그때 누적 분
    ALTER TABLE block_rule    ADD COLUMN min_minutes INTEGER NOT NULL DEFAULT 0;
    UPDATE block_rule SET min_minutes = 30, reaction = '유튜브 {minutes}분째야.'
        WHERE rule_id = 'r_yt_warn' AND reaction = '30분째야.';
    DELETE FROM usage_session WHERE duration_sec <= 1;
    """,
    # v2: 유튜브 경고를 30분마다 → 5분마다 (사용자가 직접 바꾼 값은 건드리지 않음)
    """
    UPDATE block_rule SET min_minutes = 5 WHERE rule_id = 'r_yt_warn' AND min_minutes = 30;
    """,
    # v3: 고양이의 영구 기억 — 원본은 90일 뒤 지워도 하루 요약과 채찍·당근·훈련 기억은 남는다
    """
    CREATE TABLE daily_summary (
        day           TEXT NOT NULL,                -- 현지 날짜 YYYY-MM-DD
        exe           TEXT NOT NULL,
        url_host      TEXT NOT NULL DEFAULT '',     -- '' = 웹사이트 아님 (기본키엔 NULL을 못 쓴다)
        minutes       REAL NOT NULL,                -- 자리 비움 제외 사용 분
        sessions      INTEGER NOT NULL,
        shorts_seen   INTEGER NOT NULL DEFAULT 0,   -- 숏폼 감지 횟수
        shorts_closed INTEGER NOT NULL DEFAULT 0,   -- 고양이가 실제로 닫은 횟수
        PRIMARY KEY (day, exe, url_host)
    );
    CREATE TABLE cat_memory (
        memory_id  INTEGER PRIMARY KEY,
        day        TEXT NOT NULL,                   -- 어느 날에 대한 기억인가
        kind       TEXT NOT NULL CHECK (kind IN ('carrot', 'stick', 'training')),
        reason     TEXT NOT NULL,                   -- "유튜브 어제보다 20분 줄임"
        points     INTEGER NOT NULL DEFAULT 0,      -- 당근 +, 채찍 -, 훈련 0
        created_at TEXT NOT NULL
    );
    CREATE INDEX ix_memory_day ON cat_memory(day);
    CREATE INDEX ix_event_occurred ON block_event(occurred_at);
    """,
    # v4: 단계별 반응 (말하기 → 소리 끄기 → 기다리게 → 탭 닫기) · 공부·음악 유튜브 제외
    #     SQLite는 CHECK를 ALTER로 못 바꾸므로 rule_condition 을 새로 만들어 옮긴다.
    """
    CREATE TABLE rule_condition_v4 (
        condition_id INTEGER PRIMARY KEY,
        rule_id      TEXT NOT NULL REFERENCES block_rule(rule_id) ON DELETE CASCADE,
        group_no     INTEGER NOT NULL DEFAULT 0,
        subject      TEXT NOT NULL CHECK (subject IN ('app', 'url', 'window_title')),
        operator     TEXT NOT NULL CHECK (operator IN ('eq', 'contains', 'regex', 'not_regex')),
        value        TEXT NOT NULL
    );
    INSERT INTO rule_condition_v4 SELECT * FROM rule_condition;
    DROP TABLE rule_condition;
    ALTER TABLE rule_condition_v4 RENAME TO rule_condition;

    ALTER TABLE block_event ADD COLUMN response TEXT;   -- 고양이가 실제로 한 행동: warn/mute/delay/close

    UPDATE block_rule SET name = '유튜브 (공부·음악 제외)', action = 'close' WHERE rule_id = 'r_yt_warn';
    INSERT INTO rule_condition (rule_id, group_no, subject, operator, value)
        SELECT 'r_yt_warn', 2, 'window_title', 'not_regex', '{STUDY_WORDS}'
        WHERE EXISTS (SELECT 1 FROM block_rule WHERE rule_id = 'r_yt_warn');
    UPDATE block_rule SET reaction = '또 쇼츠야?' WHERE rule_id = 'r_shorts' AND reaction = '또 보는 거야? 닫는다.';
    """.replace("{STUDY_WORDS}", STUDY_WORDS),
    # v5: "오늘 뭐 할 거야?" — 할 일별 허용 목록, 20분마다 확인, 세션마다 집중/딴짓/모름/자리비움 판정
    """
    CREATE TABLE focus_task (
        task_id    INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,                 -- "파이썬 강의", "SQLD 공부"
        created_at TEXT NOT NULL
    );
    CREATE TABLE allow_item (
        item_id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES focus_task(task_id) ON DELETE CASCADE,
        kind    TEXT NOT NULL CHECK (kind IN ('app', 'host', 'playlist', 'keyword')),
        value   TEXT NOT NULL,                           -- Code.exe / docs.python.org / PL… / 강의
        learned INTEGER NOT NULL DEFAULT 0 CHECK (learned IN (0, 1)),   -- 1 = 고양이가 물어보고 배운 것
        UNIQUE (task_id, kind, value)
    );
    CREATE TABLE focus_checkin (
        checkin_id  INTEGER PRIMARY KEY,
        task_id     INTEGER REFERENCES focus_task(task_id) ON DELETE SET NULL,
        asked_at    TEXT NOT NULL,
        answered_at TEXT,                                -- NULL = 대답 안 함
        answer      TEXT CHECK (answer IN ('focus', 'break')),
        note        TEXT                                 -- "어디까지 했어?" 에 적은 말
    );
    ALTER TABLE usage_session ADD COLUMN verdict TEXT;  -- focus / distract / unknown / away (v4 이전은 NULL)
    ALTER TABLE usage_session ADD COLUMN task_id INTEGER REFERENCES focus_task(task_id) ON DELETE SET NULL;
    ALTER TABLE daily_summary ADD COLUMN focus_minutes    REAL NOT NULL DEFAULT 0;
    ALTER TABLE daily_summary ADD COLUMN distract_minutes REAL NOT NULL DEFAULT 0;
    ALTER TABLE daily_summary ADD COLUMN unknown_minutes  REAL NOT NULL DEFAULT 0;
    """,
]

RETENTION_DAYS = 90     # 원본 기록(usage_session, block_event) 보관 기간. 요약·기억은 영구

MIN_SESSION_SEC = 1     # 이 이하 세션은 저장하지 않는다 (창을 스쳐 지나간 것)

# 숏폼 감지 기록 = '즉시 판정 규칙(min_minutes = 0)'이 발동한 기록. 유튜브 N분째 알림은 제외.
SHORTFORM_EVENT = ("rule_id IN (SELECT rule_id FROM block_rule WHERE min_minutes = 0)"
                   " AND action = 'close'")


# =============================================================================
#  DB
# =============================================================================

def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    migrate(db)
    if db.execute("SELECT COUNT(*) FROM block_rule").fetchone()[0] == 0:
        save_rules(db, DEFAULT_RULES)                # 첫 실행: 기본 규칙 심기
    summarize_and_prune(db)
    return db


def summarize_and_prune(db: sqlite3.Connection, keep_days: int = RETENTION_DAYS) -> None:
    """
    ① 어제까지 중 아직 요약 안 된 날을 daily_summary로 요약하고
    ② 요약이 끝난 날 중 keep_days 보다 오래된 원본만 지운다.
    한 트랜잭션이라 요약이 실패하면 삭제도 일어나지 않는다. 여러 번 실행해도 결과가 같다.
    """
    with db:
        db.execute("""
            INSERT INTO daily_summary (day, exe, url_host, minutes, sessions, shorts_seen, shorts_closed,
                                       focus_minutes, distract_minutes, unknown_minutes)
            SELECT day, exe, host, SUM(minutes), SUM(sessions), SUM(seen), SUM(closed),
                   SUM(focus), SUM(distract), SUM(unknown) FROM (
                SELECT date(started_at, 'localtime') AS day, exe, COALESCE(url_host, '') AS host,
                       CASE WHEN is_idle = 0 THEN duration_sec / 60.0 ELSE 0 END AS minutes,
                       1 AS sessions, 0 AS seen, 0 AS closed,
                       CASE WHEN is_idle = 0 AND verdict = 'focus'    THEN duration_sec / 60.0 ELSE 0 END AS focus,
                       CASE WHEN is_idle = 0 AND verdict = 'distract' THEN duration_sec / 60.0 ELSE 0 END AS distract,
                       CASE WHEN is_idle = 0 AND verdict = 'unknown'  THEN duration_sec / 60.0 ELSE 0 END AS unknown
                  FROM usage_session
                UNION ALL
                SELECT date(occurred_at, 'localtime'), COALESCE(exe, ''), COALESCE(url_host, ''),
                       0, 0, 1, (COALESCE(response, 'close') = 'close' AND executed = 1), 0, 0, 0
                  FROM block_event WHERE {SHORTFORM_EVENT}
            )
            WHERE day < date('now', 'localtime')
              AND day NOT IN (SELECT day FROM daily_summary)
            GROUP BY day, exe, host""".replace("{SHORTFORM_EVENT}", SHORTFORM_EVENT))
        cutoff = f"-{keep_days} days"
        db.execute("DELETE FROM usage_session WHERE date(started_at, 'localtime') < date('now', 'localtime', ?)"
                   " AND date(started_at, 'localtime') IN (SELECT day FROM daily_summary)", (cutoff,))
        db.execute("DELETE FROM block_event WHERE date(occurred_at, 'localtime') < date('now', 'localtime', ?)"
                   " AND date(occurred_at, 'localtime') IN (SELECT day FROM daily_summary)", (cutoff,))


def migrate(db: sqlite3.Connection) -> None:
    """아직 적용 안 된 MIGRATIONS를 순서대로 적용. 하나가 실패하면 그 버전은 통째로 취소된다."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    for v, script in enumerate(MIGRATIONS[version:], start=version + 1):
        db.executescript(f"BEGIN; {script} PRAGMA user_version = {v}; COMMIT;")


def save_rules(db: sqlite3.Connection, rules) -> None:
    with db:
        for r in rules:
            db.execute("INSERT INTO block_rule (rule_id, name, action, priority, reaction, min_minutes)"
                       " VALUES (?, ?, ?, ?, ?, ?)",
                       (r.rule_id, r.name, r.action, r.priority, r.reaction, r.min_minutes))
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
    return [Rule(rid, name, action, prio, tuple(conds.get(rid, ())), reaction, minutes)
            for rid, name, action, prio, reaction, minutes in db.execute(
                "SELECT rule_id, name, action, priority, reaction, min_minutes FROM block_rule"
                " WHERE enabled = 1")]


def save_session(db: sqlite3.Connection, s: Session, verdict: str | None = None,
                 task_id: int | None = None) -> None:
    if s.duration_sec <= MIN_SESSION_SEC:
        return
    with db:
        db.execute("INSERT INTO usage_session (started_at, ended_at, exe, window_title,"
                   " url_host, url, duration_sec, is_idle, verdict, task_id)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (s.started_at, s.ended_at, s.exe, s.title, _host(s.url), s.url,
                    round(s.duration_sec, 1), int(s.is_idle), verdict, task_id))


# =============================================================================
#  "오늘 뭐 할 거야?" — 할 일과 허용 목록, 집중 판정
# =============================================================================

ASK_UNKNOWN_SEC = 30          # 업무모드에서 모르는 창이 이만큼 앞에 있으면 "이것도 공부야?" 묻기
CHECKIN_SEC = 20 * 60         # 허용된 창에 이만큼 있으면 "어디야?" 확인
CHECKIN_TIMEOUT_SEC = 3 * 60  # 확인에 이만큼 대답이 없으면 그때부터 자리 비움


def _bare_host(url: str | None) -> str:
    return (_host(url) or "").lower().removeprefix("www.")


def parse_allow(text: str) -> tuple[str, str]:
    """
    사용자가 넣은 한 줄 → (종류, 값).
      유튜브 재생목록 주소(…list=PL…) → playlist   다음 영상으로 넘어가도 list= 는 그대로라서
      Code.exe                       → app
      https://docs.python.org/3/     → host        그 사이트 안에서는 주소가 바뀌어도 허용
      그 밖의 말                      → keyword     창 제목에 들어 있으면 허용
    """
    t = text.strip()
    m = re.search(r"[?&]list=([\w-]+)", t)
    if m:
        return "playlist", m.group(1)
    if t.lower().endswith(".exe"):
        return "app", t
    if "." in t and " " not in t:
        return "host", _bare_host(t)
    return "keyword", t


def is_allowed(win, items) -> bool:
    host = _bare_host(win.url)
    for kind, value in items:
        if kind == "app" and win.exe.lower() == value.lower():
            return True
        if kind == "host" and host and (host == value or host.endswith("." + value)):
            return True
        if kind == "playlist" and win.url and re.search(rf"[?&]list={re.escape(value)}(&|$)", win.url):
            return True
        if kind == "keyword" and value.lower() in (win.title or "").lower():
            return True
    return False


def window_key(win) -> tuple[str, str]:
    """고양이가 '이 창'을 기억하는 단위: 브라우저는 사이트, 나머지는 앱."""
    host = _bare_host(win.url)
    return ("host", host) if host else ("app", win.exe)


def classify(win, rules, items, decided: dict) -> str:
    """focus(업무·공부) / distract(딴짓) / unknown(모름)."""
    if pick_rule(rules, win):
        return "distract"
    if window_key(win) in decided:                       # "이번만" / "아니, 딴짓" 대답
        return decided[window_key(win)]
    if is_allowed(win, items):
        return "focus"
    if win.url and re.search(STUDY_WORDS, win.title or "", re.IGNORECASE):
        return "focus"                                   # 재생목록 없는 강의·음악 영상 (보조 수단)
    return "unknown"


def load_tasks(db: sqlite3.Connection) -> list[tuple[int, str]]:
    return db.execute("SELECT task_id, name FROM focus_task ORDER BY task_id").fetchall()


def load_allow(db: sqlite3.Connection, task_id: int) -> list[tuple[str, str]]:
    return db.execute("SELECT kind, value FROM allow_item WHERE task_id = ? ORDER BY item_id",
                      (task_id,)).fetchall()


def add_allow(db: sqlite3.Connection, task_id: int, kind: str, value: str, learned: bool = False) -> None:
    with db:
        db.execute("INSERT OR IGNORE INTO allow_item (task_id, kind, value, learned) VALUES (?, ?, ?, ?)",
                   (task_id, kind, value, int(learned)))


def get_or_create_task(db: sqlite3.Connection, name: str) -> int:
    with db:
        db.execute("INSERT OR IGNORE INTO focus_task (name, created_at) VALUES (?, ?)", (name, now_iso()))
    return db.execute("SELECT task_id FROM focus_task WHERE name = ?", (name,)).fetchone()[0]


def remove_allow(db: sqlite3.Connection, task_id: int, kind: str, value: str) -> None:
    with db:
        db.execute("DELETE FROM allow_item WHERE task_id = ? AND kind = ? AND value = ?", (task_id, kind, value))


def remember(db: sqlite3.Connection, kind: str, reason: str, points: int = 0) -> None:
    """고양이 기억(cat_memory)에 한 줄 남긴다."""
    with db:
        db.execute("INSERT INTO cat_memory (day, kind, reason, points, created_at)"
                   " VALUES (date('now', 'localtime'), ?, ?, ?, ?)", (kind, reason, points, now_iso()))


def save_event(db: sqlite3.Connection, rule: Rule, win, mode: str, response: str,
               executed: bool, minutes: int | None = None) -> None:
    with db:
        db.execute("INSERT INTO block_event (occurred_at, rule_id, action, exe, url_host,"
                   " mode, response, executed, minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (now_iso(), rule.rule_id, rule.action, win.exe, _host(win.url),
                    mode, response, int(executed), minutes))


def minutes_matching(db: sqlite3.Connection, rule: Rule, current_sec: float = 0.0) -> int:
    """
    오늘 '이 규칙에 걸리는 창'을 본 누적 분 + 지금 보고 있는 시간.
    공부·음악 영상처럼 규칙에서 빠지는 건 세지 않는다 — 그래서 사이트 단위가 아니라
    저장된 제목·주소를 규칙에 다시 대 본다.
    """
    total = current_sec
    for exe, title, url, sec in db.execute(
            "SELECT exe, window_title, url, duration_sec FROM usage_session"
            " WHERE is_idle = 0 AND date(started_at, 'localtime') = date('now', 'localtime')"):
        if rule_matches(rule, WindowInfo(title or "", exe, url)):
            total += sec
    return int(total // 60)


def times_today(db: sqlite3.Connection, rule: Rule) -> int:
    """오늘 이 규칙이 몇 번 발동했나 (쇼츠를 몇 번 열었나)."""
    return db.execute("SELECT COUNT(*) FROM block_event WHERE rule_id = ?"
                      " AND date(occurred_at, 'localtime') = date('now', 'localtime')",
                      (rule.rule_id,)).fetchone()[0]


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
    seen, closed = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(COALESCE(response, 'close') = 'close' AND executed = 1), 0)"
        f" FROM block_event WHERE {SHORTFORM_EVENT}"
        " AND date(occurred_at, 'localtime') = date('now', 'localtime')").fetchone()
    print("─" * 46 + f"\n숏폼 감지 {seen}회 · 고양이가 실제로 닫은 횟수 {closed}회")
    by = dict(db.execute("SELECT verdict, SUM(duration_sec) / 60 FROM usage_session WHERE is_idle = 0"
                         " AND date(started_at, 'localtime') = date('now', 'localtime') GROUP BY verdict"))
    print(f"📚 집중 {by.get('focus') or 0:.0f}분 · 😼 딴짓 {by.get('distract') or 0:.0f}분"
          f" · ❓ 모름 {by.get('unknown') or 0:.0f}분")


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
        self.delay_request = 0               # >0 이면 창(메인 스레드)이 그 초만큼 '기다려' 화면을 띄운다
        self.muted_exe: str | None = None    # 고양이가 소리를 끈 앱 (벗어나면 다시 켠다)
        self.task_id: int | None = None      # 업무모드에서 고른 할 일
        self.task_name = ""
        self.allow_items: list = []          # 창에서 바꿔 끼우면 루프가 다음 판정부터 따른다
        self.decided: dict = {}              # "이번만" / "아니, 딴짓" 대답 (이번 실행 동안만)
        self.ui_requests: queue.Queue = queue.Queue()   # 루프 → 창: ("unknown", key, 제목) / ("checkin",)
        self.checkin_pending = False         # "어디야?"를 물었는데 아직 대답이 없음
        self.checkin_waited = 0.0


MODES = (("log", "👀  감시 모드", "말하기만 해요. 하루 1시간 넘으면 소리를 꺼요"),
         ("close", "💼  업무모드", "말하기 → 소리 끄기 → 기다리게 → 탭 닫기"))

# 고양이 반응 단계. 규칙의 action 은 '여기까지 올라갈 수 있다'는 최대 단계다.
LADDER = ("warn", "mute", "delay", "close")
LADDER_LABEL = {"warn": "🗣️ 말하기", "mute": "🔇 소리 끔", "delay": "🚪 잠깐 기다리게 함", "close": "🐾 탭을 닫음"}
WATCH_MUTE_AFTER_MIN = 60    # 감시 모드에서도 오늘 누적 이만큼 넘으면 소리는 끈다
DELAY_SEC = 10


def decide(rule: Rule, level: int, mode: str, minutes: int) -> str:
    """
    이번에 고양이가 할 행동.
      level   : 0부터. 쇼츠는 '오늘 몇 번째 열었나', 시간 규칙은 '몇 번째 5분 구간인가'
      mode    : 'close'(업무모드)면 단계대로 올라가고, 'log'(감시 모드)면 말하기만 —
                단, 오늘 누적 WATCH_MUTE_AFTER_MIN 분이 넘으면 소리까지 끈다
      minutes : 오늘 이 규칙에 걸리는 창을 본 누적 분
    """
    ceiling = LADDER.index(rule.action) if rule.action in LADDER else 0
    if mode == "close":
        return LADDER[min(level, ceiling)]
    return "mute" if minutes >= WATCH_MUTE_AFTER_MIN and ceiling >= 1 else "warn"


def mode_name(action: str) -> str:
    return next(name for a, name, _ in MODES if a == action).strip("👀💼 ")


KIND_LABEL = {"app": "💻 앱", "host": "🌐 사이트", "playlist": "▶️ 재생목록", "keyword": "🔤 제목에"}
FONT = "맑은 고딕"


def control_window(ctl: Control, db_path: str = ":memory:") -> None:
    """앱이 켜져 있는 동안 떠 있는 창. 모드를 언제든 바꿀 수 있고, 닫으면 앱이 끝난다."""
    import signal
    import tkinter as tk
    from tkinter import ttk

    uidb = connect(db_path)                  # 창(메인 스레드) 전용 연결 — SQLite 연결은 스레드끼리 나눠 쓰지 않는다
    root = tk.Tk()
    root.title("집사 고양이")
    root.resizable(False, False)
    tk.Label(root, text="🐱 집사 고양이", font=("맑은 고딕", 12, "bold"), pady=10).pack()

    mode = tk.StringVar(value=ctl.action)

    def switch() -> None:
        ctl.action = mode.get()
        ctl.last = f"모드 변경 → {mode_name(ctl.action)}"
        print(f"=== {ctl.last} ===")
        if ctl.action == "close" and ctl.task_id is None:
            choose_task()

    def popup(title: str) -> tk.Toplevel:
        pop = tk.Toplevel(root)
        pop.title(title)
        pop.resizable(False, False)
        pop.attributes("-topmost", True)
        return pop

    def choose_task() -> None:
        """🐱 오늘 뭐 할 거야? — 할 일을 고르고 허용 목록(앱·사이트·재생목록·키워드)을 편집."""
        pop = popup("오늘 뭐 할 거야?")
        tk.Label(pop, text="🐱 오늘 뭐 할 거야?", font=(FONT, 12, "bold")).pack(pady=(12, 6))
        names = [n for _, n in load_tasks(uidb)]
        name = tk.StringVar(value=ctl.task_name or (names[-1] if names else ""))
        ttk.Combobox(pop, textvariable=name, values=names, width=30, font=(FONT, 10)).pack(padx=16)
        tk.Label(pop, text="허용 목록 — 앱(Code.exe), 사이트 주소, 유튜브 재생목록 주소, 제목 키워드",
                 fg="#555", font=(FONT, 8)).pack(pady=(10, 2))
        box = tk.Listbox(pop, width=48, height=7, font=(FONT, 9))
        box.pack(padx=16)
        shown: list[tuple[str, str]] = []

        def refresh(*_) -> None:
            box.delete(0, "end")
            shown.clear()
            n = name.get().strip()
            tid = next((i for i, t in load_tasks(uidb) if t == n), None)
            shown.extend(load_allow(uidb, tid) if tid else [])
            for kind, value in shown:
                box.insert("end", f"{KIND_LABEL[kind]}  {value}")

        def add(*_) -> None:
            if name.get().strip() and entry.get().strip():
                add_allow(uidb, get_or_create_task(uidb, name.get().strip()), *parse_allow(entry.get()))
                entry.delete(0, "end")
                refresh()

        def remove() -> None:
            n = name.get().strip()
            for i in box.curselection():
                remove_allow(uidb, get_or_create_task(uidb, n), *shown[i])
            refresh()

        def start() -> None:
            n = name.get().strip()
            if not n:
                return
            ctl.task_id = get_or_create_task(uidb, n)
            ctl.task_name = n
            ctl.allow_items = load_allow(uidb, ctl.task_id)
            ctl.last = f"📚 '{n}' 시작! 20분마다 어디인지 물어볼게"
            print(f"=== 할 일: {n} ({len(ctl.allow_items)}개 허용) ===")
            pop.destroy()

        row = tk.Frame(pop)
        row.pack(pady=6)
        entry = tk.Entry(row, width=36, font=(FONT, 9))
        entry.pack(side="left")
        entry.bind("<Return>", add)
        tk.Button(row, text="추가", command=add).pack(side="left", padx=4)
        tk.Button(pop, text="선택 삭제", command=remove).pack()
        tk.Button(pop, text="시작", width=14, font=(FONT, 10, "bold"), command=start).pack(pady=10)
        name.trace_add("write", refresh)
        refresh()

    def ask_unknown(key: tuple[str, str], title: str) -> None:
        """모르는 창이 30초 넘게 앞에 있으면: 🐱 이것도 공부야?"""
        kind, value = key
        pop = popup("이것도 공부야?")
        tk.Label(pop, text=f"🐱 {value}\n{title[:40]}\n\n이것도 '{ctl.task_name}' 하는 중이야?",
                 font=(FONT, 10), padx=20, pady=12, justify="center").pack()

        def answer(choice: str) -> None:
            if choice == "remember":
                add_allow(uidb, ctl.task_id, kind, value, learned=True)
                ctl.allow_items = load_allow(uidb, ctl.task_id)
                remember(uidb, "training", f"'{ctl.task_name}' 할 때 {value}도 공부라고 배움")
                ctl.last = f"🧠 {value} 기억했어"
            else:
                ctl.decided[key] = "focus" if choice == "once" else "distract"
            pop.destroy()

        row = tk.Frame(pop)
        row.pack(pady=(0, 12))
        for text, choice in (("응, 기억해", "remember"), ("이번만", "once"), ("아니, 딴짓", "distract")):
            tk.Button(row, text=text, width=10, command=lambda c=choice: answer(c)).pack(side="left", padx=3)

    def ask_checkin() -> None:
        """허용된 창에 20분 있으면: 🐱 어디야? — 3분 안에 대답 없으면 그때부터 자리 비움."""
        with uidb:
            cid = uidb.execute("INSERT INTO focus_checkin (task_id, asked_at) VALUES (?, ?)",
                               (ctl.task_id, now_iso())).lastrowid
        pop = popup("어디야?")
        tk.Label(pop, text=f"🐱 어디야?\n아직 '{ctl.task_name}' 하는 중이야?",
                 font=(FONT, 11, "bold"), padx=20, pady=10).pack()
        tk.Label(pop, text="어디까지 했어? (안 적어도 돼)", fg="#555", font=(FONT, 9)).pack()
        note = tk.Entry(pop, width=34, font=(FONT, 9))
        note.pack(padx=16, pady=4)

        def answer(a: str) -> None:
            with uidb:
                uidb.execute("UPDATE focus_checkin SET answered_at = ?, answer = ?, note = ? WHERE checkin_id = ?",
                             (now_iso(), a, note.get().strip() or None, cid))
            ctl.checkin_pending = False
            ctl.last = "📚 좋아, 계속 가 보자!" if a == "focus" else "☕ 푹 쉬고 와"
            pop.destroy()

        row = tk.Frame(pop)
        row.pack(pady=(4, 12))
        tk.Button(row, text="📚 하는 중", width=11, command=lambda: answer("focus")).pack(side="left", padx=4)
        tk.Button(row, text="☕ 쉬는 중", width=11, command=lambda: answer("break")).pack(side="left", padx=4)

    for action, name, desc in MODES:
        tk.Radiobutton(root, text=f"{name}\n{desc}", variable=mode, value=action,
                       command=switch, indicatoron=False, selectcolor="#ffe8a3",
                       width=30, pady=8, font=("맑은 고딕", 10)).pack(padx=20, pady=3)

    task_label = tk.Label(root, font=(FONT, 9, "bold"), pady=4)
    task_label.pack()
    tk.Button(root, text="📚 할 일 고르기", font=(FONT, 9), command=choose_task).pack()
    status = tk.Label(root, fg="#555", wraplength=260, pady=10, font=("맑은 고딕", 9))
    status.pack()
    tk.Label(root, text="창을 닫으면 고양이도 쉽니다", fg="#999", font=("맑은 고딕", 8)).pack(pady=(0, 8))

    def show_delay(seconds: int) -> None:
        """화면 전체를 덮는 '잠깐 기다려' 창. seconds 뒤에 스스로 사라진다."""
        cover = tk.Toplevel(root)
        cover.attributes("-fullscreen", True)
        cover.attributes("-topmost", True)
        cover.attributes("-alpha", 0.92)
        cover.configure(bg="#1b1b1b")
        text = tk.Label(cover, fg="white", bg="#1b1b1b", font=("맑은 고딕", 30, "bold"))
        text.pack(expand=True)

        def count(n: int) -> None:
            if n <= 0:
                cover.destroy()
                return
            text.config(text=f"🐱 잠깐!\n\n{n}초만 참아 봐")
            cover.after(1000, count, n - 1)
        count(seconds)

    def tick() -> None:                      # 작업 스레드 소식을 0.5초마다 창에 반영
        status.config(text=ctl.last)
        task_label.config(text=f"할 일: {ctl.task_name}" if ctl.task_name else "할 일: (업무모드에서 골라요)")
        while not ctl.ui_requests.empty():
            req = ctl.ui_requests.get_nowait()
            if req[0] == "unknown":
                ask_unknown(req[1], req[2])
            elif req[0] == "checkin":
                ask_checkin()
        if ctl.delay_request:
            seconds, ctl.delay_request = ctl.delay_request, 0
            show_delay(seconds)
        if ctl.stop.is_set():                # 감시 루프가 오류로 멈춘 경우
            root.destroy()
            return
        root.after(500, tick)

    # 터미널 Ctrl+C로도 끌 수 있게 (Tk 대기 중에는 KeyboardInterrupt가 전달되지 않음)
    signal.signal(signal.SIGINT, lambda *_: root.after(0, root.destroy))
    root.lift()
    root.focus_force()
    if ctl.action == "close":
        root.after(300, choose_task)         # 업무모드로 시작하면 바로 "오늘 뭐 할 거야?"
    root.after(0, tick)                      # 창이 다 뜬 뒤에 시작 — 감시 루프가 이미 멈췄어도 깔끔하게 닫히게
    root.mainloop()
    uidb.close()


def respond(probe, ctl: Control, response: str, win) -> bool:
    """행동을 실제로 한다. 성공하면 True."""
    if response == "mute":
        ok = probe.mute_app(win.exe, True)
        if ok:
            ctl.muted_exe = win.exe
        return ok
    if response == "delay":
        ctl.delay_request = DELAY_SEC        # 화면은 메인 스레드(창)가 띄운다
        return True
    if response == "close":
        return block(probe, win)
    return True                              # warn: 말하기는 항상 성공


def act(db: sqlite3.Connection, probe, ctl: Control, rule: Rule, win, level: int,
        minutes: int, timed: bool) -> None:
    """규칙 발동: 단계 결정 → 행동 → 모드·행동·성공 여부 기록 → 고양이 대사."""
    response = decide(rule, level, ctl.action, minutes)
    executed = respond(probe, ctl, response, win)
    save_event(db, rule, win, ctl.action, response, executed, minutes if timed else None)
    line = rule.reaction.replace("{minutes}", str(minutes))
    ctl.last = f"🐱 {line} — {LADDER_LABEL[response]}" + ("" if executed else " (실패)")
    print(f"  {ctl.last}")


def run(db: sqlite3.Connection, probe, interval: float, ctl: Control, stop_when_empty: bool) -> None:
    rules = load_rules(db)
    instant = [r for r in rules if r.min_minutes == 0]      # 창을 열자마자 판정
    timed = [r for r in rules if r.min_minutes > 0]         # 오늘 누적 N분마다 판정
    # ponytail: 같은 구간에서 두 번 말하지 않게 메모리에만 기억. 하루에 앱을 다시 켜면 현재 구간을 한 번 더 말한다.
    fired: set[tuple] = set()
    streak = 0.0                          # 마지막 "어디야?" 이후 허용된 창에 있은 시간
    unknown_for: dict = {}                # 모르는 창(사이트/앱)별로 앞에 있던 시간
    asked: set = set()                    # 이번 실행에서 이미 물어본 창

    def on_session_end(s: Session) -> None:
        win_ = WindowInfo(s.title, s.exe, s.url)
        verdict = "away" if s.is_idle else classify(win_, rules, ctl.allow_items, ctl.decided)
        save_session(db, s, verdict, ctl.task_id)

    tracker = SessionTracker(on_session_end)
    try:
        while not ctl.stop.is_set():
            win = probe.probe()
            if win is None and stop_when_empty:
                break
            verdict = classify(win, rules, ctl.allow_items, ctl.decided) if win else None
            if ctl.action == "close" and ctl.task_id is not None and win is not None:
                if verdict == "unknown":                    # 모르는 창 → 30초 넘으면 한 번 물어봄
                    key = window_key(win)
                    unknown_for[key] = unknown_for.get(key, 0) + interval
                    if unknown_for[key] >= ASK_UNKNOWN_SEC and key not in asked:
                        asked.add(key)
                        ctl.ui_requests.put(("unknown", key, win.title))
                if verdict == "focus" and not ctl.checkin_pending:   # 허용된 창 20분 → "어디야?"
                    streak += interval
                    if streak >= CHECKIN_SEC:
                        streak = 0.0
                        ctl.checkin_pending, ctl.checkin_waited = True, 0.0
                        ctl.ui_requests.put(("checkin",))
            if ctl.checkin_pending:
                if ctl.action != "close":                   # 감시 모드로 바꾸면 "어디야?" 대기도 그만 — 자리 비움으로 세지 않는다
                    ctl.checkin_pending = False
                else:
                    ctl.checkin_waited += interval
            # 자리 비움: "어디야?"에 3분 넘게 대답 없음 — 또는 입력이 없는데 공부 창도 아님.
            # 공부 창(강의 영상 등)은 입력이 없어도 보고 있을 수 있어서 입력으로는 판단하지 않는다.
            idle = ((ctl.checkin_pending and ctl.checkin_waited >= CHECKIN_TIMEOUT_SEC)
                    or (probe.idle_seconds() > IDLE_THRESHOLD_SEC and verdict != "focus"))
            if tracker.observe(win, interval, idle) and win is not None:
                print(f"[{now_iso()}] {win.exe:<14} {win.title[:50]}")
                rule = pick_rule(instant, win)
                if rule:                                    # 쇼츠: 오늘 몇 번째인지가 단계
                    act(db, probe, ctl, rule, win, level=times_today(db, rule),
                        minutes=minutes_matching(db, rule), timed=False)
            if win is not None and not idle and tracker.current:
                for rule in timed:                          # 유튜브: 몇 번째 5분 구간인지가 단계
                    if not rule_matches(rule, win):
                        continue
                    minutes = minutes_matching(db, rule, tracker.current.duration_sec)
                    step = minutes // rule.min_minutes
                    key = (rule.rule_id, date.today(), step)
                    if step >= 1 and key not in fired:
                        fired.add(key)
                        act(db, probe, ctl, rule, win, level=step - 1,
                            minutes=step * rule.min_minutes, timed=True)
            # 소리를 끈 뒤 규칙에 걸리지 않는 창(강의·다른 앱)으로 옮기면 소리를 돌려준다
            if ctl.muted_exe and not (win and any(rule_matches(r, win) for r in rules)):
                unmute(probe, ctl)
            if not stop_when_empty:                         # 시뮬레이션·테스트는 기다리지 않고 바로 다음 틱
                ctl.stop.wait(interval)
    except KeyboardInterrupt:
        print("\n중단됨.")
    finally:
        tracker.flush()
        if ctl.muted_exe:                                   # 고양이가 쉬러 가도 크롬이 음소거로 남지 않게
            unmute(probe, ctl)


def unmute(probe, ctl: Control) -> None:
    # ponytail: 앱이 강제 종료되면 소리가 꺼진 채로 남는다 — 그땐 Windows 볼륨 믹서에서 켜면 된다.
    probe.mute_app(ctl.muted_exe, False)
    print(f"  🔈 {ctl.muted_exe} 소리 다시 켬")
    ctl.last = "🔈 소리를 다시 켰어요"
    ctl.muted_exe = None


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
        control_window(ctl, args.db)         # 창이 닫힐 때까지 여기서 대기
        ctl.stop.set()
        worker.join(timeout=5)               # 마지막 세션 저장을 기다린다
        db = connect(args.db)

    print_report(db)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
