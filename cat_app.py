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
from dataclasses import replace
import time
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
    # v6: 유튜브 영상 종류 자동 분류 (YouTube 카테고리 → 강의/노래/딴짓, 애매하면 사용자에게 물어봄)
    """
    CREATE TABLE video_info (
        video_id   TEXT PRIMARY KEY,                     -- watch?v= 뒤 11자
        category   TEXT,                                 -- YouTube 카테고리 원문 ("Music"), '' = 못 읽음
        user_label TEXT CHECK (user_label IN ('lecture', 'music', 'fun')),   -- "이거 강의 맞아?" 대답
        fetched_at TEXT NOT NULL
    );
    CREATE TABLE rule_condition_v6 (
        condition_id INTEGER PRIMARY KEY,
        rule_id      TEXT NOT NULL REFERENCES block_rule(rule_id) ON DELETE CASCADE,
        group_no     INTEGER NOT NULL DEFAULT 0,
        subject      TEXT NOT NULL CHECK (subject IN ('app', 'url', 'window_title', 'video_kind')),
        operator     TEXT NOT NULL CHECK (operator IN ('eq', 'contains', 'regex', 'not_regex')),
        value        TEXT NOT NULL
    );
    INSERT INTO rule_condition_v6 SELECT * FROM rule_condition;
    DROP TABLE rule_condition;
    ALTER TABLE rule_condition_v6 RENAME TO rule_condition;
    UPDATE rule_condition SET subject = 'video_kind', value = '^(lecture|music|ask)$'
        WHERE rule_id = 'r_yt_warn' AND subject = 'window_title' AND operator = 'not_regex';
    ALTER TABLE usage_session ADD COLUMN video_kind TEXT;
    """,
    # v7: 단계 간격을 분 → 초 단위로 (딴짓 영상 5분 → 10초마다). 업무모드 쇼츠는 첫 번째부터 바로 닫는다(코드).
    """
    ALTER TABLE block_rule ADD COLUMN step_sec INTEGER NOT NULL DEFAULT 0;
    UPDATE block_rule SET step_sec = min_minutes * 60;
    UPDATE block_rule SET step_sec = 10, reaction = '딴짓 영상 {time}째야.'
        WHERE rule_id = 'r_yt_warn' AND min_minutes = 5;
    ALTER TABLE block_event ADD COLUMN seconds INTEGER;    -- 누적 시간 규칙이면 그때 누적 초 (minutes 대신)
    """,
]

RETENTION_DAYS = 90     # 원본 기록(usage_session, block_event) 보관 기간. 요약·기억은 영구

MIN_SESSION_SEC = 1     # 이 이하 세션은 저장하지 않는다 (창을 스쳐 지나간 것)

# 숏폼 감지 기록 = '즉시 판정 규칙(step_sec = 0)'이 발동한 기록. 딴짓 영상 누적 알림은 제외.
# response 가 없는 건 v4 이전 옛 기록 — 단계·횟수 계산에 섞이면 첫 쇼츠부터 바로 닫아 버린다.
SHORTFORM_EVENT = ("response IS NOT NULL AND rule_id IN (SELECT rule_id FROM block_rule WHERE step_sec = 0)"
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
            db.execute("INSERT INTO block_rule (rule_id, name, action, priority, reaction, step_sec)"
                       " VALUES (?, ?, ?, ?, ?, ?)",
                       (r.rule_id, r.name, r.action, r.priority, r.reaction, r.step_sec))
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
    return [Rule(rid, name, action, prio, tuple(conds.get(rid, ())), reaction, step)
            for rid, name, action, prio, reaction, step in db.execute(
                "SELECT rule_id, name, action, priority, reaction, step_sec FROM block_rule"
                " WHERE enabled = 1")]


def save_session(db: sqlite3.Connection, s: Session, verdict: str | None = None,
                 task_id: int | None = None, video_kind: str | None = None) -> None:
    if s.duration_sec <= MIN_SESSION_SEC:
        return
    with db:
        db.execute("INSERT INTO usage_session (started_at, ended_at, exe, window_title,"
                   " url_host, url, duration_sec, is_idle, verdict, task_id, video_kind)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (s.started_at, s.ended_at, s.exe, s.title, _host(s.url), s.url,
                    round(s.duration_sec, 1), int(s.is_idle), verdict, task_id, video_kind))


# =============================================================================
#  유튜브 영상 종류 — YouTube 카테고리로 자동 분류, 애매하면 물어본다
# =============================================================================

CATEGORY_KIND = {
    "Music": "music",
    "Education": "lecture", "Science & Technology": "lecture", "Howto & Style": "lecture",
    "Gaming": "fun", "Comedy": "fun", "Sports": "fun", "Pets & Animals": "fun",
    "Autos & Vehicles": "fun", "Travel & Events": "fun",
    # 그 밖(Entertainment, People & Blogs, Film & Animation, News & Politics …)과 못 읽은 경우는 애매 → ask
}
VIDEO_LABEL = {"lecture": "📚 강의", "music": "🎵 노래", "fun": "😼 딴짓 영상"}
ASK_VIDEO_SEC = 10            # 애매한 영상을 이만큼 보면 "이거 강의 맞아?"


def video_id(url: str | None) -> str | None:
    m = re.search(r"[?&]v=([\w-]{11})", url or "")
    return m.group(1) if m else None


def video_kind(category: str | None, user_label: str | None, title: str, mode: str) -> str | None:
    """
    영상 종류: lecture / music / fun / ask(물어봐야 함) / None(카테고리 가져오는 중).
    우선순위: 사용자 대답 > YouTube 카테고리 > 제목 키워드(강의·노래 단어).
    감시 모드는 묻지 않으므로 애매하면 딴짓으로 본다.
    """
    if user_label:
        return user_label
    if category is None:
        return None
    kind = CATEGORY_KIND.get(category, "ask")
    if kind == "ask" and re.search(STUDY_WORDS, title or "", re.IGNORECASE):
        return "lecture"
    if kind == "ask" and mode != "close":
        return "fun"
    return kind


def fetch_category(vid: str) -> str:
    """영상 페이지에서 YouTube 카테고리를 읽는다 (API 키 불필요). 실패하면 ''."""
    import urllib.request
    try:
        req = urllib.request.Request(f"https://www.youtube.com/watch?v={vid}",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
        html = urllib.request.urlopen(req, timeout=5).read().decode("utf-8", "replace")
        m = re.search(r'"category":"([^"]+)"', html)
        return m.group(1).replace("\\u0026", "&") if m else ""
    except Exception:                                    # noqa: BLE001 — 네트워크 없음 등: 애매로 처리
        return ""


def load_video_labels(db: sqlite3.Connection) -> dict:
    return {vid: (cat, label) for vid, cat, label in
            db.execute("SELECT video_id, category, user_label FROM video_info")}


def save_video(db: sqlite3.Connection, vid: str, category: str | None = None, user_label: str | None = None) -> None:
    """카테고리나 사용자 대답을 저장. 이미 있으면 준 값만 덮어쓴다."""
    with db:
        db.execute("INSERT INTO video_info (video_id, category, user_label, fetched_at) VALUES (?, ?, ?, ?)"
                   " ON CONFLICT(video_id) DO UPDATE SET"
                   " category = COALESCE(excluded.category, category),"
                   " user_label = COALESCE(excluded.user_label, user_label)",
                   (vid, category, user_label, now_iso()))


# =============================================================================
#  "오늘 뭐 할 거야?" — 할 일과 허용 목록, 집중 판정
# =============================================================================

ASK_UNKNOWN_SEC = 30          # 업무모드에서 모르는 창이 이만큼 앞에 있으면 "이것도 공부야?" 묻기
AUTO_TASK = "자동 분류"         # 고양이가 배운 앱·사이트를 담는 목록 (할 일을 따로 고르지 않는다)
CHECKIN_SEC = 20 * 60         # 허용된 창에 이만큼 있으면 "어디야?" 확인
CHECKIN_TIMEOUT_SEC = 3 * 60  # 확인에 이만큼 대답이 없으면 그때부터 자리 비움


def _bare_host(url: str | None) -> str:
    return (_host(url) or "").lower().removeprefix("www.")


# 자동 분류용 내장 목록. 여기 없는 창은 업무모드에서 30초 뒤 "이것도 공부야?" 하고 물어 배운다.
FOCUS_APPS = {
    "code.exe", "pycharm64.exe", "idea64.exe", "devenv.exe", "arduino ide.exe", "rstudio.exe", "matlab.exe",
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "notepad.exe", "notepad++.exe",
    "obsidian.exe", "notion.exe", "onenote.exe", "zotero.exe",
    "winword.exe", "excel.exe", "powerpnt.exe", "hwp.exe", "acrord32.exe", "acrobat.exe", "sumatrapdf.exe",
}
DISTRACT_APPS = {"steam.exe", "leagueclient.exe", "league of legends.exe", "riotclientservices.exe",
                 "battle.net.exe", "epicgameslauncher.exe"}
FOCUS_HOSTS = {
    "github.com", "stackoverflow.com", "stackexchange.com", "python.org", "developer.mozilla.org",
    "w3schools.com", "wikipedia.org", "notion.so", "notion.site", "claude.ai", "chatgpt.com",
    "gemini.google.com", "colab.research.google.com", "kaggle.com", "dataq.or.kr", "q-net.or.kr",
    "inflearn.com", "coursera.org", "udemy.com", "khanacademy.org", "scholar.google.com", "dbpia.co.kr",
    "riss.kr", "arxiv.org", "music.youtube.com", "ac.kr",          # ac.kr = 대학 사이트 전부
}
FOCUS_HOST_PREFIXES = ("docs.",)                                   # docs.python.org, docs.google.com …
DISTRACT_HOSTS = {
    "netflix.com", "twitch.tv", "chzzk.naver.com", "sooplive.co.kr", "afreecatv.com", "tiktok.com",
    "instagram.com", "facebook.com", "x.com", "twitter.com", "comic.naver.com", "fmkorea.com",
    "dcinside.com", "reddit.com", "tving.com", "wavve.com", "coupangplay.com", "disneyplus.com",
}


def _host_in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_allowed(win, items) -> bool:
    """고양이가 배운 목록(앱·사이트)에 있나."""
    host = _bare_host(win.url)
    for kind, value in items:
        if kind == "app" and win.exe.lower() == value.lower():
            return True
        if kind == "host" and host and _host_in(host, (value,)):
            return True
    return False


def builtin_kind(win) -> str | None:
    """내장 목록으로 본 창 종류: focus / distract / None(모름)."""
    host, exe = _bare_host(win.url), win.exe.lower()
    if host:
        if _host_in(host, FOCUS_HOSTS) or host.startswith(FOCUS_HOST_PREFIXES):
            return "focus"
        if _host_in(host, DISTRACT_HOSTS):
            return "distract"
        return None
    if exe in FOCUS_APPS:
        return "focus"
    if exe in DISTRACT_APPS:
        return "distract"
    return None


def window_key(win) -> tuple[str, str]:
    """고양이가 '이 창'을 기억하는 단위: 브라우저는 사이트, 나머지는 앱."""
    host = _bare_host(win.url)
    return ("host", host) if host else ("app", win.exe)


def classify(win, rules, items, decided: dict) -> str:
    """focus(업무·공부) / distract(딴짓) / unknown(모름)."""
    if pick_rule(rules, win):
        return "distract"
    if getattr(win, "video_kind", None) in ("lecture", "music"):   # 유튜브 카테고리/대답으로 판단
        return "focus"
    if window_key(win) in decided:                       # "이번만" / "아니, 딴짓" 대답
        return decided[window_key(win)]
    if is_allowed(win, items):
        return "focus"
    if builtin_kind(win):
        return builtin_kind(win)
    if win.url and not video_id(win.url) and re.search(STUDY_WORDS, win.title or "", re.IGNORECASE):
        return "focus"                                   # 유튜브 밖 강의 사이트 등 (보조 수단)
    return "unknown"


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


def remember(db: sqlite3.Connection, kind: str, reason: str, points: int = 0) -> None:
    """고양이 기억(cat_memory)에 한 줄 남긴다."""
    with db:
        db.execute("INSERT INTO cat_memory (day, kind, reason, points, created_at)"
                   " VALUES (date('now', 'localtime'), ?, ?, ?, ?)", (kind, reason, points, now_iso()))


def save_event(db: sqlite3.Connection, rule: Rule, win, mode: str, response: str,
               executed: bool, seconds: int | None = None) -> None:
    with db:
        db.execute("INSERT INTO block_event (occurred_at, rule_id, action, exe, url_host,"
                   " mode, response, executed, seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (now_iso(), rule.rule_id, rule.action, win.exe, _host(win.url),
                    mode, response, int(executed), seconds))


def seconds_matching(db: sqlite3.Connection, rule: Rule, current_sec: float = 0.0) -> int:
    """
    오늘 '이 규칙에 걸리는 창'을 본 누적 초 + 지금 보고 있는 시간.
    공부·음악 영상처럼 규칙에서 빠지는 건 세지 않는다 — 그래서 사이트 단위가 아니라
    저장된 제목·주소를 규칙에 다시 대 본다.
    """
    total = current_sec
    for exe, title, url, sec, kind in db.execute(
            "SELECT exe, window_title, url, duration_sec, video_kind FROM usage_session"
            " WHERE is_idle = 0 AND date(started_at, 'localtime') = date('now', 'localtime')"):
        if rule_matches(rule, WindowInfo(title or "", exe, url, video_kind=kind)):
            total += sec
    return int(total)


def fmt_time(sec: int) -> str:
    """40 → '40초', 130 → '2분 10초', 600 → '10분'."""
    m, s = divmod(int(sec), 60)
    return f"{m}분 {s}초" if m and s else (f"{m}분" if m else f"{s}초")


def top_apps_today(db: sqlite3.Connection, limit: int = 5, since: str | None = None) -> list[tuple[str, float]]:
    """설계서 Q2: 오늘(현지 날짜) 가장 많이 쓴 앱. 자리 비움은 뺀다. since(UTC)를 주면 그 뒤로만."""
    return db.execute(
        "SELECT exe, SUM(duration_sec) AS total FROM usage_session"
        " WHERE is_idle = 0 AND date(started_at, 'localtime') = date('now', 'localtime')"
        " AND started_at >= ? GROUP BY exe ORDER BY total DESC LIMIT ?", (since or "", limit)).fetchall()


VERDICT_LABEL = {"focus": "📚 집중", "distract": "😼 딴짓", "unknown": "❓ 모름", "away": "💤 자리 비움"}


def today_breakdown(db: sqlite3.Connection, top: int = 5) -> dict:
    """
    오늘 판정별로 '무엇을' 했나: {판정: [(사이트/앱, 분, [많이 본 제목…]), …]}.
    v5 이전 기록(판정 없음)은 뺀다.
    """
    agg: dict = {}
    for verdict, what, title, sec in db.execute(
            "SELECT verdict, COALESCE(url_host, exe), window_title, duration_sec FROM usage_session"
            " WHERE verdict IS NOT NULL AND date(started_at, 'localtime') = date('now', 'localtime')"):
        total, titles = agg.setdefault(verdict, {}).setdefault(what, [0.0, {}])
        agg[verdict][what][0] = total + sec
        titles[title or ""] = titles.get(title or "", 0) + sec
    return {v: sorted(((what, sec / 60, [t for t, _ in sorted(ts.items(), key=lambda x: -x[1]) if t][:2])
                       for what, (sec, ts) in items.items()), key=lambda r: -r[1])[:top]
            for v, items in agg.items()}


def breakdown_text(db: sqlite3.Connection) -> str:
    parts = []
    data = today_breakdown(db)
    for verdict, label in VERDICT_LABEL.items():
        rows = data.get(verdict, [])
        parts.append(f"{label} {sum(m for _, m, _ in rows):.0f}분")
        for what, minutes, titles in rows:
            parts.append(f"    {what}  {minutes:.0f}분" + (f" — {' · '.join(t[:30] for t in titles)}" if titles else ""))
    return "\n".join(parts)


def print_report(db: sqlite3.Connection, since: str | None = None) -> None:
    """since(이번에 켠 시각, UTC)가 있으면 '이번 실행'과 '오늘 전체'를 나눠 보여 준다."""
    blocks = ([("이번에 켠 뒤", since)] if since else []) + [("오늘 전체 (앞서 켰던 것 포함)", None)]
    for title, start in blocks:
        rows = top_apps_today(db, since=start)
        print("\n" + "─" * 46 + f"\n{title} — 가장 많이 쓴 앱 TOP 5\n" + "─" * 46)
        for exe, sec in rows:
            print(f"  {exe:<24} {sec / 60:>6.1f}분")
        if not rows:
            print("  (기록 없음)")
    seen, closed = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(COALESCE(response, 'close') = 'close' AND executed = 1), 0)"
        f" FROM block_event WHERE {SHORTFORM_EVENT}"
        " AND date(occurred_at, 'localtime') = date('now', 'localtime')").fetchone()
    print("─" * 46 + f"\n숏폼 감지 {seen}회 · 고양이가 실제로 닫은 횟수 {closed}회")
    print("─" * 46 + "\n오늘 한 일\n" + breakdown_text(db))


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
        self.task_id: int | None = None      # '자동 분류' 목록 번호 (루프가 시작할 때 정한다)
        self.allow_items: list = []          # 고양이가 배운 앱·사이트 — 창에서 배우면 바로 바꿔 끼운다
        self.decided: dict = {}              # "이번만" / "아니, 딴짓" 대답 (이번 실행 동안만)
        self.ui_requests: queue.Queue = queue.Queue()   # 루프 → 창: ("unknown", key, 제목) / ("checkin",)
        self.video_labels: dict = {}         # 영상 ID → (YouTube 카테고리, 사용자 대답)
        self.fetched: queue.Queue = queue.Queue()        # 카테고리 가져오기 결과 (가져오는 스레드 → 루프)
        self.checkin_pending = False         # "어디야?"를 물었는데 아직 대답이 없음
        self.checkin_waited = 0.0


MODES = (("log", "👀  감시 모드", "기록하고, 가끔 말만 해요"),
         ("close", "💼  업무모드", "딴짓하면 고양이가 막아요"))

# 고양이 반응 단계. 규칙의 action 은 '여기까지 올라갈 수 있다'는 최대 단계다.
LADDER = ("warn", "mute", "delay", "close")
SAID = {"warn": "", "mute": "소리 껐어.", "delay": "10초만 참아.", "close": "꺼 버렸어."}
THREAT = {"mute": "소리 안 들리게 할 거야", "delay": "10초 기다리게 할 거야", "close": "꺼 버릴 거야"}


WATCH_MUTE_AFTER_SEC = 60 * 60   # 감시 모드에서도 오늘 누적 이만큼 넘으면 소리는 끈다
DELAY_SEC = 10


def heads_up(rule: Rule, level: int, mode: str, seconds: int) -> str:
    """고양이의 다음 예고: '10초 더 보면 소리 안 들리게 할 거야' / '업무 중엔 쇼츠 금지야'."""
    ceiling = LADDER.index(rule.action) if rule.action in LADDER else 0
    if mode == "close":
        if rule.step_sec == 0:                              # 쇼츠: 업무모드에선 바로 닫는다
            return "업무 중엔 쇼츠 금지야." if ceiling == LADDER.index("close") else ""
        nxt = LADDER[min(level + 1, ceiling)]
        if nxt == "warn":
            return ""
        if nxt == LADDER[min(level, ceiling)] == "close":
            return "계속 보면 계속 꺼 버릴 거야."
        return f"{fmt_time(rule.step_sec)} 더 보면 {THREAT[nxt]}."
    if ceiling >= 1 and seconds < WATCH_MUTE_AFTER_SEC:    # 감시 모드: 1시간 되면 소리만
        return f"{-(-(WATCH_MUTE_AFTER_SEC - seconds) // 60)}분 더 보면 {THREAT['mute']}."
    return ""


def decide(rule: Rule, level: int, mode: str, seconds: int) -> str:
    """
    이번에 고양이가 할 행동.
      업무모드('close'): 쇼츠처럼 즉시 규칙(step_sec = 0)은 바로 최대 단계(닫기).
                        딴짓 영상처럼 시간 규칙은 level(몇 번째 step_sec 구간인가, 0부터)만큼 단계를 올린다.
      감시 모드('log')  : 말하기만. 단, 오늘 누적 WATCH_MUTE_AFTER_SEC 이 넘으면 소리까지 끈다.
      seconds: 오늘 이 규칙에 걸리는 창을 본 누적 초
    """
    ceiling = LADDER.index(rule.action) if rule.action in LADDER else 0
    if mode == "close":
        return LADDER[ceiling] if rule.step_sec == 0 else LADDER[min(level, ceiling)]
    return "mute" if seconds >= WATCH_MUTE_AFTER_SEC and ceiling >= 1 else "warn"


def mode_name(action: str) -> str:
    return next(name for a, name, _ in MODES if a == action).strip("👀💼 ")


FONT = "맑은 고딕"


def control_window(ctl: Control, db_path: str = ":memory:") -> None:
    """앱이 켜져 있는 동안 떠 있는 창. 모드를 언제든 바꿀 수 있고, 닫으면 앱이 끝난다."""
    import signal
    import tkinter as tk

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

    def popup(title: str) -> tk.Toplevel:
        pop = tk.Toplevel(root)
        pop.title(title)
        pop.resizable(False, False)
        pop.attributes("-topmost", True)
        return pop

    def ask_unknown(key: tuple[str, str], title: str) -> None:
        """모르는 창이 30초 넘게 앞에 있으면: 🐱 이것도 공부야?"""
        kind, value = key
        pop = popup("이것도 공부야?")
        tk.Label(pop, text=f"🐱 {value}\n{title[:40]}\n\n이것도 공부·업무야?",
                 font=(FONT, 10), padx=20, pady=12, justify="center").pack()

        def answer(choice: str) -> None:
            if choice == "remember":
                if ctl.task_id is None:
                    return
                add_allow(uidb, ctl.task_id, kind, value, learned=True)
                ctl.allow_items = load_allow(uidb, ctl.task_id)
                remember(uidb, "training", f"{value}도 공부·업무라고 배움")
                ctl.last = f"🧠 {value} 기억했어"
            else:
                ctl.decided[key] = "focus" if choice == "once" else "distract"
            pop.destroy()

        row = tk.Frame(pop)
        row.pack(pady=(0, 12))
        for text, choice in (("응, 기억해", "remember"), ("이번만", "once"), ("아니, 딴짓", "distract")):
            tk.Button(row, text=text, width=10, command=lambda c=choice: answer(c)).pack(side="left", padx=3)

    def ask_video(vid: str, title: str) -> None:
        """애매한 유튜브 영상을 10초 넘게 보면: 🐱 이거 강의 맞아?"""
        pop = popup("이거 강의 맞아?")
        tk.Label(pop, text=f"🐱 이거 강의 맞아?\n\n{title[:45]}", font=(FONT, 10), padx=20, pady=12).pack()

        def answer(label: str) -> None:
            save_video(uidb, vid, user_label=label)
            ctl.video_labels[vid] = (ctl.video_labels.get(vid, (None, None))[0], label)
            remember(uidb, "training", f"'{title[:25]}' → {VIDEO_LABEL[label]}(이)라고 배움")
            ctl.last = f"🧠 {VIDEO_LABEL[label]}(으)로 기억했어"
            pop.destroy()

        row = tk.Frame(pop)
        row.pack(pady=(0, 12))
        for text, label in (("📚 강의야", "lecture"), ("🎵 노래야", "music"), ("😼 딴짓이야", "fun")):
            tk.Button(row, text=text, width=10, command=lambda l=label: answer(l)).pack(side="left", padx=3)

    def ask_checkin() -> None:
        """허용된 창에 20분 있으면: 🐱 어디야? — 3분 안에 대답 없으면 그때부터 자리 비움."""
        with uidb:
            cid = uidb.execute("INSERT INTO focus_checkin (task_id, asked_at) VALUES (?, ?)",
                               (ctl.task_id, now_iso())).lastrowid
        pop = popup("어디야?")
        tk.Label(pop, text=f"🐱 어디야?\n아직 공부·업무 중이야?",
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

    def show_today() -> None:
        """📊 오늘 한 일 — 집중·딴짓·모름·자리 비움별로 무엇을 몇 분 했나."""
        pop = popup("오늘 한 일")
        text = tk.Text(pop, width=60, height=20, font=(FONT, 9), padx=10, pady=8)
        text.pack()

        def refresh() -> None:
            text.config(state="normal")
            text.delete("1.0", "end")
            text.insert("end", breakdown_text(uidb) + "\n\n(지금 보고 있는 창은 다른 창으로 옮겨야 더해져요)")
            text.config(state="disabled")
        tk.Button(pop, text="새로고침", command=refresh).pack(pady=6)
        refresh()

    row = tk.Frame(root)
    row.pack()
    tk.Button(row, text="📊 오늘 한 일", font=(FONT, 9), command=show_today).pack(side="left", padx=2)
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
        while not ctl.ui_requests.empty():
            req = ctl.ui_requests.get_nowait()
            if req[0] == "unknown":
                ask_unknown(req[1], req[2])
            elif req[0] == "video":
                ask_video(req[1], req[2])
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
        seconds: int, timed: bool) -> None:
    """규칙 발동: 단계 결정 → 행동 → 모드·행동·성공 여부 기록 → 고양이 대사."""
    response = decide(rule, level, ctl.action, seconds)
    executed = respond(probe, ctl, response, win)
    save_event(db, rule, win, ctl.action, response, executed, seconds if timed else None)
    line = rule.reaction.replace("{time}", fmt_time(seconds)).replace("{minutes}", str(seconds // 60))
    words = [line, SAID[response] if executed else "", heads_up(rule, level, ctl.action, seconds)]
    ctl.last = "🐱 " + " ".join(w for w in words if w)
    print(f"  {ctl.last}")


def run(db: sqlite3.Connection, probe, interval: float, ctl: Control, stop_when_empty: bool,
        fetch=fetch_category) -> None:
    rules = load_rules(db)
    ctl.video_labels.update(load_video_labels(db))
    if ctl.task_id is None:
        ctl.task_id = get_or_create_task(db, AUTO_TASK)
        ctl.allow_items = load_allow(db, ctl.task_id)
    fetching: set = set()                 # 카테고리를 가져오는 중인 영상
    watched_for: dict = {}                # 애매한 영상별로 본 시간
    asked_videos: set = set()

    def take_fetched() -> None:
        """가져온 카테고리를 DB와 메모리에 반영."""
        while not ctl.fetched.empty():
            vid_, category_ = ctl.fetched.get_nowait()
            save_video(db, vid_, category=category_)
            ctl.video_labels[vid_] = (category_, ctl.video_labels.get(vid_, (None, None))[1])

    def kind_of(w, title: str = "") -> str | None:
        """이 창이 유튜브 영상이면 종류. 처음 보는 영상은 카테고리를 가져오기 시작한다."""
        vid = video_id(w.url if w else None)
        if not vid:
            return None
        category, user_label = ctl.video_labels.get(vid, (None, None))
        if category is None and user_label is None and vid not in fetching:
            fetching.add(vid)
            if stop_when_empty:                          # 시뮬레이션·테스트: 바로 가져와 바로 반영
                ctl.fetched.put((vid, fetch(vid)))
                take_fetched()
                category, user_label = ctl.video_labels[vid]
            else:                                        # 실제: 감시가 멈추지 않게 뒤에서
                threading.Thread(target=lambda: ctl.fetched.put((vid, fetch(vid))), daemon=True).start()
        return video_kind(category, user_label, title or w.title, ctl.action)
    instant = [r for r in rules if r.step_sec == 0]         # 창을 열자마자 판정
    timed = [r for r in rules if r.step_sec > 0]            # 오늘 누적 N초마다 판정
    # ponytail: 같은 구간에서 두 번 말하지 않게 메모리에만 기억. 하루에 앱을 다시 켜면 현재 구간을 한 번 더 말한다.
    fired: set[tuple] = set()
    streak = 0.0                          # 마지막 "어디야?" 이후 허용된 창에 있은 시간
    unknown_for: dict = {}                # 모르는 창(사이트/앱)별로 앞에 있던 시간
    asked: set = set()                    # 이번 실행에서 이미 물어본 창

    def on_session_end(s: Session) -> None:
        win_ = WindowInfo(s.title, s.exe, s.url)
        win_ = replace(win_, video_kind=kind_of(win_))
        verdict = "away" if s.is_idle else classify(win_, rules, ctl.allow_items, ctl.decided)
        save_session(db, s, verdict, ctl.task_id, win_.video_kind)

    tracker = SessionTracker(on_session_end)
    last_tick = time.monotonic()
    try:
        while not ctl.stop.is_set():
            win = probe.probe()
            if win is None and stop_when_empty:
                break
            # 한 번 살피는 데 걸린 '실제' 시간 (주소 읽기 등으로 interval보다 길다).
            # PC가 절전에 들어갔다 깨면 그 공백은 세지 않는다. 시뮬레이션·테스트는 interval 그대로.
            now = time.monotonic()
            elapsed = interval if stop_when_empty else min(now - last_tick, max(5 * interval, 5.0))
            last_tick = now
            if win is not None and win.pid == os.getpid():  # 고양이 창에 대답하는 중 — 세지도 묻지도 않는다
                if not stop_when_empty:
                    ctl.stop.wait(interval)
                continue
            take_fetched()                                  # 뒤에서 가져온 카테고리 반영
            if win is not None:
                win = replace(win, video_kind=kind_of(win))
                if win.video_kind == "ask" and ctl.action == "close":   # 애매한 영상 10초 → "이거 강의 맞아?"
                    vid = video_id(win.url)
                    watched_for[vid] = watched_for.get(vid, 0) + elapsed
                    if watched_for[vid] >= ASK_VIDEO_SEC and vid not in asked_videos:
                        asked_videos.add(vid)
                        ctl.ui_requests.put(("video", vid, win.title))
            verdict = classify(win, rules, ctl.allow_items, ctl.decided) if win else None
            if ctl.action == "close" and win is not None:
                if verdict == "unknown" and "youtube.com" not in _bare_host(win.url):
                    # 모르는 창 → 30초 넘으면 한 번 물어봄 (유튜브는 영상마다 따로 물으니 사이트 통째로는 안 물음)
                    key = window_key(win)
                    unknown_for[key] = unknown_for.get(key, 0) + elapsed
                    if unknown_for[key] >= ASK_UNKNOWN_SEC and key not in asked:
                        asked.add(key)
                        ctl.ui_requests.put(("unknown", key, win.title))
                if verdict == "focus" and not ctl.checkin_pending:   # 허용된 창 20분 → "어디야?"
                    streak += elapsed
                    if streak >= CHECKIN_SEC:
                        streak = 0.0
                        ctl.checkin_pending, ctl.checkin_waited = True, 0.0
                        ctl.ui_requests.put(("checkin",))
            if ctl.checkin_pending:
                if ctl.action != "close":                   # 감시 모드로 바꾸면 "어디야?" 대기도 그만 — 자리 비움으로 세지 않는다
                    ctl.checkin_pending = False
                else:
                    ctl.checkin_waited += elapsed
            # 자리 비움: "어디야?"에 3분 넘게 대답 없음 — 또는 입력이 없는데 공부 창도 아님.
            # 공부 창(강의 영상 등)은 입력이 없어도 보고 있을 수 있어서 입력으로는 판단하지 않는다.
            idle = ((ctl.checkin_pending and ctl.checkin_waited >= CHECKIN_TIMEOUT_SEC)
                    or (probe.idle_seconds() > IDLE_THRESHOLD_SEC and verdict != "focus"))
            if tracker.observe(win, elapsed, idle) and win is not None:
                print(f"[{now_iso()}] {win.exe:<14} {win.title[:50]}")
                rule = pick_rule(instant, win)
                if rule:                                    # 쇼츠: 오늘 몇 번째인지가 단계
                    act(db, probe, ctl, rule, win, level=0,
                        seconds=seconds_matching(db, rule), timed=False)
            if win is not None and not idle and tracker.current:
                for rule in timed:                          # 딴짓 영상: 10초 구간을 넘을 때마다 한 단계씩
                    if not rule_matches(rule, win):
                        continue
                    seconds = seconds_matching(db, rule, tracker.current.duration_sec)
                    step = seconds // rule.step_sec
                    key = (rule.rule_id, date.today(), step)
                    if step >= 1 and key not in fired:
                        # 단계는 '오늘 몇 번째 반응인가' — 카테고리를 늦게 알아서 시간이 건너뛰어도
                        # 말하기 → 소리 → 기다리게 → 닫기를 한 칸씩 밟는다.
                        level = sum(1 for k in fired if k[:2] == key[:2])
                        fired.add(key)
                        act(db, probe, ctl, rule, win, level=level,
                            seconds=step * rule.step_sec, timed=True)
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
        db = None
        try:
            db = connect(db_path)
            run(db, WindowsProbe(), interval, ctl, stop_when_empty=False)
        except Exception as e:                           # noqa: BLE001
            ctl.last = f"⚠ 오류로 멈춤: {type(e).__name__}: {e}"
            print(ctl.last)
            ctl.stop.set()
        finally:
            if db is not None:
                db.close()


def main() -> int:
    # 한국어 Windows 콘솔(cp949)에서 이모지·특수문자 출력으로 죽지 않게
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

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
        run(db, SimulatedProbe(), 0.05, Control("log"), stop_when_empty=True,
            fetch=lambda vid: "Entertainment")           # 시뮬레이션은 인터넷에 묻지 않는다
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
        started = now_iso()
        print(f"=== {mode_name(ctl.action)} 시작 (DB: {args.db}) — 고양이 창을 닫으면 종료 ===")
        worker = threading.Thread(target=watch_in_background,
                                  args=(args.db, args.interval, ctl), daemon=True)
        worker.start()
        control_window(ctl, args.db)         # 창이 닫힐 때까지 여기서 대기
        ctl.stop.set()
        worker.join(timeout=5)               # 마지막 세션 저장을 기다린다
        db = connect(args.db)

    print_report(db, since=started if not (args.simulate or args.report) else None)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
