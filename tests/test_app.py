#!/usr/bin/env python3
"""cat_app DB 계층 테스트 — python tests/test_app.py (Windows 없이 동작)"""

import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))   # 상위 폴더의 cat_app·watch를 찾도록

import cat_app

from cat_app import (Control, block, connect, load_rules, run, save_session,
                     single_instance, top_apps_today)
from watch import Session, WindowInfo, now_iso, pick_rule

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 앱처럼 cp949 콘솔에서 이모지 출력 허용

SHORTS = WindowInfo("웃긴영상 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/shorts/x")
NORMAL = WindowInfo("강의 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/watch?v=a")   # 공부 → 제외
FUN = WindowInfo("웃긴 고양이 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/watch?v=ffffffffff1",
                 video_kind="fun")


class RecordingProbe:
    """행동을 실제로 하지 않고 무엇을 했는지만 적어 두는 가짜 프로브."""
    def __init__(self, frames, on_frame=None):
        self.frames, self.on_frame, self.calls = list(frames), on_frame, []
    def probe(self):
        if self.on_frame:
            self.on_frame(len(self.frames))
        return self.frames.pop(0) if self.frames else None
    def idle_seconds(self):
        return 0.0
    def close_tab(self, hwnd):
        self.calls.append("close"); return True
    def mute_app(self, exe, mute):
        self.calls.append("mute" if mute else "unmute"); return True


class TestDb(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def test_rules_survive_db_round_trip(self):
        """DB에서 읽은 규칙이 스파이크 기본 규칙과 똑같이 판정해야 한다 (AND/OR 그룹 포함)."""
        rules = load_rules(self.db)
        self.assertEqual(pick_rule(rules, SHORTS).rule_id, "r_shorts")
        self.assertEqual(pick_rule(rules, FUN).rule_id, "r_yt_warn")
        self.assertIsNone(pick_rule(rules, NORMAL))           # 강의는 not_regex 조건으로 빠짐

    def test_default_rules_seeded(self):
        n = self.db.execute("SELECT COUNT(*) FROM block_rule").fetchone()[0]
        self.assertEqual(n, 3)

    def test_disabled_rule_is_ignored(self):
        self.db.execute("UPDATE block_rule SET enabled = 0 WHERE rule_id IN ('r_shorts', 'r_reels')")
        self.assertIsNone(pick_rule(load_rules(self.db), SHORTS))    # 쇼츠는 영상 종류가 없어서 유튜브 규칙도 안 걸림

    def test_bad_action_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO block_rule (rule_id, name, action) VALUES ('x', 'x', 'explode')")

    def test_top_apps_skips_idle_and_stores_host_only(self):
        t = now_iso()
        save_session(self.db, Session(t, "Code.exe", "main.py", None, 30.0, False, t))
        save_session(self.db, Session(t, "chrome.exe", "yt", "https://youtube.com/shorts/x?t=1", 10.0, False, t))
        save_session(self.db, Session(t, "chrome.exe", "yt", None, 999.0, True, t))   # 자리 비움
        self.assertEqual(top_apps_today(self.db), [("Code.exe", 30.0), ("chrome.exe", 10.0)])
        hosts = [h for (h,) in self.db.execute("SELECT url_host FROM usage_session WHERE url_host IS NOT NULL")]
        self.assertEqual(hosts, ["youtube.com"])


class TestBlock(unittest.TestCase):
    """브라우저는 탭만, 다른 앱은 창을 닫아야 한다 (크롬 창 전체가 날아가면 안 됨)."""

    class FakeProbe:
        def __init__(self):
            self.calls = []
        def close_tab(self, hwnd):
            self.calls.append("tab"); return True
        def close_window(self, hwnd):
            self.calls.append("window"); return True

    def test_browser_closes_tab_only(self):
        p = self.FakeProbe()
        block(p, SHORTS)
        block(p, WindowInfo("쇼츠 - Edge", "MSEDGE.EXE", "youtube.com/shorts/x"))
        self.assertEqual(p.calls, ["tab", "tab"])

    def test_other_app_closes_window(self):
        p = self.FakeProbe()
        block(p, WindowInfo("게임", "game.exe"))
        self.assertEqual(p.calls, ["window"])


class TestLadder(unittest.TestCase):
    """업무모드: 쇼츠는 바로 닫기, 딴짓 영상은 10초마다 말하기 → 소리 끄기 → 기다리게 → 탭 닫기."""

    def test_decide(self):
        rules = {r.rule_id: r for r in load_rules(connect(":memory:"))}
        shorts, fun = rules["r_shorts"], rules["r_yt_warn"]
        self.assertEqual([cat_app.decide(shorts, lv, "close", 0) for lv in range(3)], ["close"] * 3)
        self.assertEqual([cat_app.decide(fun, lv, "close", 0) for lv in range(5)],
                         ["warn", "mute", "delay", "close", "close"])
        self.assertEqual(cat_app.decide(shorts, 3, "log", 3599), "warn")    # 감시 모드: 말하기만
        self.assertEqual(cat_app.decide(shorts, 0, "log", 3600), "mute")    # 1시간 넘으면 소리 끔

    def test_shorts_closed_at_once_in_work_mode(self):
        ctl = Control("log")
        shorts = [WindowInfo(f"쇼츠{i}", "chrome.exe", f"youtube.com/shorts/{i}") for i in range(4)]
        # 첫 쇼츠는 감시 모드에서 보고, 그다음 업무모드로 바꾼다
        probe = RecordingProbe(shorts, on_frame=lambda left: setattr(ctl, "action", "close") if left == 3 else None)
        db = connect(":memory:")
        run(db, probe, 0.0, ctl, stop_when_empty=True)
        self.assertEqual(db.execute("SELECT mode, response, executed FROM block_event ORDER BY event_id").fetchall(),
                         [("log", "warn", 1)] + [("close", "close", 1)] * 3)
        self.assertEqual(probe.calls, ["close"] * 3)                 # 업무모드 첫 쇼츠부터 바로 닫음

    def test_fun_video_steps_every_10_seconds(self):
        db = connect(":memory:")
        ctl = Control("close")
        probe = RecordingProbe([FUN] * 5 + [WindowInfo("main.py - VS Code", "Code.exe")])
        run(db, probe, 10.0, ctl, True, fetch=lambda vid: "Comedy")
        self.assertEqual(db.execute("SELECT seconds, response FROM block_event ORDER BY event_id").fetchall(),
                         [(10, "warn"), (20, "mute"), (30, "delay"), (40, "close"), (50, "close")])
        self.assertEqual(probe.calls, ["mute", "close", "close", "unmute"])   # VS Code로 옮기자 소리를 돌려줌
        self.assertEqual(ctl.delay_request, cat_app.DELAY_SEC)

    def test_never_skips_a_step(self):
        """이미 35초 본 상태(카테고리를 늦게 알았거나 아까 본 것)여도 첫 반응은 말하기부터 한 칸씩."""
        db = connect(":memory:")
        t = now_iso()
        save_session(db, Session(t, "chrome.exe", "예능", "youtube.com/watch?v=aaaaaaaaaaa", 35, False, t),
                     video_kind="fun")
        run(db, RecordingProbe([FUN] * 2), 10.0, Control("close"), True, fetch=lambda vid: "Comedy")
        self.assertEqual(db.execute("SELECT seconds, response FROM block_event ORDER BY event_id").fetchall(),
                         [(40, "warn"), (50, "mute")])


class TestTalkAndCounts(unittest.TestCase):
    """숏폼 2개 봤는데 25개로 뜨던 버그 · 고양이 예고 · 오늘 한 일."""

    def test_old_version_records_do_not_raise_the_step(self):
        db = connect(":memory:")
        for _ in range(23):                                    # 옛 버전 기록: response 없음
            db.execute("INSERT INTO block_event (occurred_at, rule_id, action) VALUES (?, 'r_shorts', 'close')",
                       (now_iso(),))
        db.commit()
        shorts = [WindowInfo(f"쇼츠{i}", "chrome.exe", f"youtube.com/shorts/{i}") for i in range(2)]
        run(db, RecordingProbe(shorts), 0.0, Control("close"), True)
        self.assertEqual(db.execute("SELECT response FROM block_event WHERE response IS NOT NULL"
                                    " ORDER BY event_id").fetchall(), [("close",), ("close",)])
        seen = db.execute(f"SELECT COUNT(*) FROM block_event WHERE {cat_app.SHORTFORM_EVENT}").fetchone()[0]
        self.assertEqual(seen, 2)                                                              # 리포트도 2회

    def test_cat_warns_what_comes_next(self):
        rules = {r.rule_id: r for r in load_rules(connect(":memory:"))}
        H = cat_app.heads_up
        self.assertEqual(H(rules["r_shorts"], 0, "close", 0), "업무 중엔 쇼츠 금지야.")
        self.assertEqual(H(rules["r_shorts"], 0, "log", 0), "60분 더 보면 소리 안 들리게 할 거야.")
        self.assertEqual(H(rules["r_yt_warn"], 0, "close", 10), "10초 더 보면 소리 안 들리게 할 거야.")
        self.assertEqual(H(rules["r_yt_warn"], 2, "close", 30), "10초 더 보면 꺼 버릴 거야.")
        self.assertEqual(H(rules["r_yt_warn"], 3, "close", 40), "계속 보면 계속 꺼 버릴 거야.")
        self.assertEqual(H(rules["r_yt_warn"], 0, "log", 15 * 60), "45분 더 보면 소리 안 들리게 할 거야.")
        self.assertEqual([cat_app.fmt_time(x) for x in (40, 130, 600)], ["40초", "2분 10초", "10분"])

    def test_message_has_no_process_labels(self):
        ctl = Control("close")
        run(connect(":memory:"), RecordingProbe([SHORTS]), 0.0, ctl, True)
        self.assertEqual(ctl.last, "🐱 또 쇼츠야? 꺼 버렸어. 업무 중엔 쇼츠 금지야.")

    def test_today_breakdown_lists_what_was_done(self):
        db = connect(":memory:")
        t = now_iso()
        for title, exe, url, sec, verdict in (
                ("main.py - VS Code", "Code.exe", None, 900, "focus"),
                ("파이썬 강의 21강", "chrome.exe", "youtube.com/watch?v=a&list=PL1", 600, "focus"),
                ("웃긴 영상", "chrome.exe", "youtube.com/watch?v=b", 300, "distract"),
                ("옛 기록", "x.exe", None, 999, None)):                       # v5 이전 기록은 빠짐
            save_session(db, Session(t, exe, title, url, sec, False, t), verdict)
        b = cat_app.today_breakdown(db)
        self.assertEqual(b["focus"], [("Code.exe", 15.0, ["main.py - VS Code"]),
                                      ("youtube.com", 10.0, ["파이썬 강의 21강"])])
        self.assertEqual(b["distract"], [("youtube.com", 5.0, ["웃긴 영상"])])
        self.assertNotIn(None, b)
        self.assertIn("📚 집중 25분", cat_app.breakdown_text(db))

    def test_cat_own_window_is_ignored(self):
        """고양이 창(오늘 뭐 할 거야? 등)에 대답하는 시간은 모름으로 세지 않고, 묻지도 않는다."""
        db = connect(":memory:")
        ctl = Control("close")
        ctl.task_id = cat_app.get_or_create_task(db, "파이썬 강의")
        own = WindowInfo("오늘 뭐 할 거야?", "python.exe", pid=os.getpid())
        run(db, RecordingProbe([own] * 10), 10.0, ctl, True)
        self.assertTrue(ctl.ui_requests.empty())
        self.assertEqual(db.execute("SELECT COUNT(*) FROM usage_session").fetchone()[0], 0)

    def test_report_splits_this_run_from_today(self):
        db = connect(":memory:")
        db.execute("INSERT INTO usage_session (started_at, exe, duration_sec, is_idle)"
                   " VALUES (strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-5 seconds'), 'WindowsTerminal.exe', 1500, 0)")
        started = now_iso()
        save_session(db, Session(started, "WindowsTerminal.exe", "t", None, 300, False, started), "unknown")
        self.assertEqual(cat_app.top_apps_today(db, since=started), [("WindowsTerminal.exe", 300.0)])
        self.assertEqual(cat_app.top_apps_today(db), [("WindowsTerminal.exe", 1800.0)])


class TestVideoKind(unittest.TestCase):
    """HATENA(노래)처럼 제목에 단서가 없어도 YouTube 카테고리로 강의·노래·딴짓을 나눈다."""

    def test_mapping(self):
        K = cat_app.video_kind
        self.assertEqual(K("Music", None, "HATENA", "close"), "music")
        self.assertEqual(K("Education", None, "1강", "close"), "lecture")
        self.assertEqual(K("Gaming", None, "롤 하이라이트", "close"), "fun")
        self.assertEqual(K("Entertainment", None, "예능", "close"), "ask")        # 애매 → 업무모드는 물어봄
        self.assertEqual(K("Entertainment", None, "예능", "log"), "fun")          # 감시 모드는 안 묻고 딴짓
        self.assertEqual(K("Entertainment", None, "파이썬 강의 3편", "close"), "lecture")   # 제목 키워드 보조
        self.assertEqual(K("", None, "?", "close"), "ask")                         # 카테고리를 못 읽음
        self.assertEqual(K("Gaming", "lecture", "게임 개발 강좌", "close"), "lecture")    # 사용자 대답이 최우선
        self.assertIsNone(K(None, None, "가져오는 중", "close"))
        self.assertEqual(cat_app.video_id("youtube.com/watch?v=dQw4w9WgXcQ&list=PL1"), "dQw4w9WgXcQ")
        self.assertIsNone(cat_app.video_id("youtube.com/shorts/abc"))

    def video(self, vid, title="HATENA - YouTube - Chrome"):
        return WindowInfo(title, "chrome.exe", f"https://www.youtube.com/watch?v={vid}")

    def test_music_is_focus_and_never_warned(self):
        db = connect(":memory:")
        t = now_iso()
        save_session(db, Session(t, "chrome.exe", "노래", "youtube.com/watch?v=mmmmmmmmmm0", 3600, False, t),
                     video_kind="music")                                          # 오늘 노래 1시간
        ctl = Control("close")
        run(db, RecordingProbe([self.video("mmmmmmmmmm1")] * 30), 10.0, ctl, True, fetch=lambda v: "Music")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM block_event").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT verdict, video_kind FROM usage_session ORDER BY session_id DESC"
                                    " LIMIT 1").fetchone(), ("focus", "music"))
        self.assertEqual(db.execute("SELECT category FROM video_info WHERE video_id = 'mmmmmmmmmm1'").fetchone(),
                         ("Music",))                                              # 한 번 읽은 카테고리는 저장

    def test_ambiguous_video_asks_in_work_mode(self):
        db = connect(":memory:")
        ctl = Control("close")
        v = self.video("eeeeeeeeee1", "예능 모음 - YouTube - Chrome")
        run(db, RecordingProbe([v] * 3), 10.0, ctl, True, fetch=lambda vid: "Entertainment")
        self.assertEqual(ctl.ui_requests.get_nowait(), ("video", "eeeeeeeeee1", v.title))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM block_event").fetchone()[0], 0)   # 대답 전엔 벌 안 줌

    def test_session_keeps_latest_title(self):
        """유튜브는 주소가 먼저 바뀌고 제목이 몇 초 뒤에 바뀐다 → 기록엔 나중 제목(진짜 제목)이 남아야 한다."""
        db = connect(":memory:")
        url = "https://www.youtube.com/watch?v=UW1a3h9Hlf4"
        frames = [WindowInfo("メルト - YouTube", "chrome.exe", url)] * 2 + [WindowInfo("HATENA - YouTube", "chrome.exe", url)] * 3
        run(db, RecordingProbe(frames), 1.0, Control("log"), True, fetch=lambda v: "Music")
        self.assertEqual(db.execute("SELECT window_title FROM usage_session").fetchall(), [("HATENA - YouTube",)])

    def test_answer_is_remembered(self):
        db = connect(":memory:")
        cat_app.save_video(db, "eeeeeeeeee2", category="Entertainment")
        cat_app.save_video(db, "eeeeeeeeee2", user_label="lecture")               # "📚 강의야"
        ctl = Control("close")
        fetched = []
        run(db, RecordingProbe([self.video("eeeeeeeeee2", "예능?")] * 3), 10.0, ctl, True,
            fetch=lambda vid: fetched.append(vid) or "Entertainment")
        self.assertEqual(fetched, [])                                             # 다시 읽지도, 묻지도 않음
        self.assertTrue(ctl.ui_requests.empty())
        self.assertEqual(db.execute("SELECT category, user_label FROM video_info").fetchone(),
                         ("Entertainment", "lecture"))


class TestTimedRule(unittest.TestCase):
    """딴짓 영상은 오늘 누적 10초 구간마다 한 번 반응한다 (강의·노래 시간은 안 셈)."""

    def run_on(self, fun_minutes, mode, study_minutes=0):
        db = connect(":memory:")
        t = now_iso()
        save_session(db, Session(t, "chrome.exe", "예능 - YouTube", "youtube.com/watch?v=aaaaaaaaaaa",
                                 fun_minutes * 60, False, t), video_kind="fun")
        if study_minutes:
            save_session(db, Session(t, "chrome.exe", "파이썬 강의 - YouTube", "youtube.com/watch?v=sssssssssss",
                                     study_minutes * 60, False, t), video_kind="lecture")
        probe = RecordingProbe([FUN] * 3)
        ctl = Control(mode)
        run(db, probe, 0.0, ctl, stop_when_empty=True, fetch=lambda vid: "Comedy")
        return db, probe, ctl

    def test_once_per_step_in_watch_mode(self):
        db, probe, ctl = self.run_on(30.5, "log")
        self.assertEqual(db.execute("SELECT seconds, response FROM block_event").fetchall(), [(1830, "warn")])
        self.assertIn("딴짓 영상 30분 30초째야", ctl.last)

    def test_study_time_is_not_counted(self):
        db, _, _ = self.run_on(7, "log", study_minutes=50)          # 공부 50분은 세지 않음 → 딴짓 7분만
        self.assertEqual(db.execute("SELECT seconds FROM block_event").fetchall(), [(420,)])

    def test_watch_mode_mutes_after_an_hour(self):
        db, probe, _ = self.run_on(61, "log")
        self.assertEqual(db.execute("SELECT response FROM block_event").fetchall(), [("mute",)])
        self.assertEqual(probe.calls, ["mute", "unmute"])            # 끝날 때 소리를 돌려줌


class TestSessionsAndMigration(unittest.TestCase):
    def test_short_sessions_skipped_and_full_url_kept(self):
        db = connect(":memory:")
        t = now_iso()
        save_session(db, Session(t, "Code.exe", "main.py", None, 1.0, False, t))      # 1초 이하 → 버림
        save_session(db, Session(t, "chrome.exe", "쇼츠", "youtube.com/shorts/x", 5.0, False, t))  # 크롬 주소창 모양
        self.assertEqual(db.execute("SELECT exe, url_host, url FROM usage_session").fetchall(),
                         [("chrome.exe", "youtube.com", "youtube.com/shorts/x")])

    def test_old_db_is_upgraded(self):
        """v0(이전 버전) DB를 열면 컬럼이 추가되고, 1초 이하 기록이 지워지고, 경고 규칙이 바뀐다."""
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.executescript(cat_app.SCHEMA)                      # 버전 0 모양
        old.execute("INSERT INTO block_rule (rule_id, name, action, priority, reaction)"
                    " VALUES ('r_yt_warn', '유튜브 경고', 'warn', 50, '30분째야.')")
        old.executemany("INSERT INTO usage_session (started_at, exe, duration_sec, is_idle)"
                        " VALUES ('2026-09-26T00:00:00Z', 'x.exe', ?, 0)", [(1.0,), (5.0,)])
        old.commit(); old.close()

        db = connect(path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], len(cat_app.MIGRATIONS))
        self.assertEqual(db.execute("SELECT duration_sec FROM usage_session").fetchall(), [(5.0,)])
        self.assertEqual(db.execute("SELECT reaction, step_sec, action FROM block_rule").fetchone(),
                         ("딴짓 영상 {time}째야.", 10, "close"))                # v7: 10초마다
        self.assertEqual(db.execute("SELECT operator FROM rule_condition WHERE rule_id = 'r_yt_warn'").fetchall(),
                         [("not_regex",)])                     # v4: 강의·노래 제외 조건이 붙음
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"focus_task", "allow_item", "focus_checkin", "video_info"} <= tables)   # v5, v6
        self.assertEqual(db.execute("SELECT subject, value FROM rule_condition WHERE rule_id = 'r_yt_warn'").fetchall(),
                         [("video_kind", "^(lecture|music|ask)$")])             # v6: 제목 대신 영상 종류
        db.close()
        connect(path).close()                                  # 두 번 열어도 다시 적용되지 않음


def days_ago(n: int) -> str:
    """현지 날짜로 n일 전 정오를 UTC 문자열로 — 자정 직후처럼 UTC 날짜와 현지 날짜가 다를 때도 정확하게."""
    local_noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=n)
    return local_noon.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestMemoryAndRetention(unittest.TestCase):
    """원본은 90일 뒤 지워도, 하루 요약과 고양이 기억은 남아야 한다."""

    def add(self, db, n_days_ago, host, sec, shorts=0, closed=0):
        t = days_ago(n_days_ago)
        db.execute("INSERT INTO usage_session (started_at, ended_at, exe, url_host, duration_sec, is_idle)"
                   " VALUES (?, ?, 'chrome.exe', ?, ?, 0)", (t, t, host, sec))
        for i in range(shorts):
            db.execute("INSERT INTO block_event (occurred_at, rule_id, action, exe, url_host, mode, response, executed)"
                       " VALUES (?, 'r_shorts', 'close', 'chrome.exe', ?, 'close', 'close', ?)", (t, host, int(i < closed)))
        db.commit()

    def test_summarize_then_prune(self):
        db = connect(":memory:")
        self.add(db, 100, "youtube.com", 600, shorts=3, closed=2)   # 90일 넘음 → 요약 후 원본 삭제
        self.add(db, 1, "youtube.com", 120)                         # 어제 → 요약되지만 원본 유지
        self.add(db, 0, "youtube.com", 60)                          # 오늘 → 아직 요약 안 함
        cat_app.summarize_and_prune(db)
        cat_app.summarize_and_prune(db)                             # 두 번 돌려도 중복 없음

        summary = db.execute("SELECT day, minutes, sessions, shorts_seen, shorts_closed FROM daily_summary"
                             " ORDER BY day").fetchall()
        self.assertEqual([r[1:] for r in summary], [(10.0, 1, 3, 2), (2.0, 1, 0, 0)])
        self.assertEqual(summary[0][0], days_ago(100)[:10])
        # 원본: 100일 전 것만 지워짐
        self.assertEqual(db.execute("SELECT COUNT(*) FROM usage_session").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM block_event").fetchone()[0], 0)

    def test_nothing_deleted_if_summary_fails(self):
        """요약 단계가 실패하면 원본 삭제도 일어나지 않아야 한다 (한 트랜잭션)."""
        db = connect(":memory:")
        self.add(db, 100, "youtube.com", 600)
        db.execute("DROP TABLE daily_summary")                  # 요약이 반드시 실패하게
        with self.assertRaises(sqlite3.OperationalError):
            cat_app.summarize_and_prune(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM usage_session").fetchone()[0], 1)

    def test_memory_kinds(self):
        db = connect(":memory:")
        db.execute("INSERT INTO cat_memory (day, kind, reason, points, created_at)"
                   " VALUES ('2026-09-27', 'carrot', '유튜브 어제보다 20분 줄임', 10, ?)", (now_iso(),))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO cat_memory (day, kind, reason, created_at)"
                       " VALUES ('2026-09-27', 'fish', '?', ?)", (now_iso(),))


class TestFocus(unittest.TestCase):
    """오늘 뭐 할 거야? — 허용 목록, 모르는 창 묻기, 20분마다 어디야?"""

    def test_host_allows_whole_site(self):
        items = [("host", "python.org")]
        self.assertTrue(cat_app.is_allowed(WindowInfo("t", "chrome.exe", "docs.python.org/3/library/re.html"), items))
        self.assertFalse(cat_app.is_allowed(WindowInfo("t", "chrome.exe", "notpython.org/x"), items))

    def test_builtin_auto_classification(self):
        """할 일을 고르지 않아도 흔한 공부·업무 앱/사이트와 딴짓 사이트는 자동으로 나뉜다."""
        B = cat_app.builtin_kind
        self.assertEqual(B(WindowInfo("main.py", "Code.exe")), "focus")
        self.assertEqual(B(WindowInfo("과제.hwp", "Hwp.exe")), "focus")
        self.assertEqual(B(WindowInfo("t", "chrome.exe", "github.com/x")), "focus")
        self.assertEqual(B(WindowInfo("t", "chrome.exe", "docs.python.org/3/")), "focus")
        self.assertEqual(B(WindowInfo("t", "chrome.exe", "eclass.kangwon.ac.kr/x")), "focus")   # 대학 사이트
        self.assertEqual(B(WindowInfo("t", "chrome.exe", "www.netflix.com/browse")), "distract")
        self.assertEqual(B(WindowInfo("롤", "LeagueClient.exe")), "distract")
        self.assertIsNone(B(WindowInfo("t", "chrome.exe", "someblog.net/post")))
        self.assertIsNone(B(WindowInfo("t", "Discord.exe")))

    def test_classify_order(self):
        rules = load_rules(connect(":memory:"))
        so = WindowInfo("질문", "chrome.exe", "someblog.net/q/1")
        self.assertEqual(cat_app.classify(SHORTS, rules, [("host", "youtube.com")], {}), "distract")  # 규칙이 먼저
        self.assertEqual(cat_app.classify(so, rules, [], {}), "unknown")
        self.assertEqual(cat_app.classify(so, rules, [], {("host", "someblog.net"): "focus"}), "focus")
        self.assertEqual(cat_app.classify(so, rules, [("host", "someblog.net")], {}), "focus")

    def test_ask_unknown_then_checkin_then_away(self):
        """할 일을 고르지 않아도: 모르는 사이트 30초 → 묻기, 공부 앱 20분 → 어디야?, 무응답 3분 → 자리 비움."""
        db = connect(":memory:")
        ctl = Control("close")                                  # 할 일 없이 업무모드
        so = WindowInfo("질문 - 어떤 블로그", "chrome.exe", "someblog.net/q/1")
        code = WindowInfo("main.py - VS Code", "Code.exe")
        # 10초 간격: 모르는 창 30초 → 허용된 창 20분 → 대답 없이 3분 넘게 더
        frames = [so] * 3 + [code] * 120 + [code] * 20
        run(db, RecordingProbe(frames), 10.0, ctl, stop_when_empty=True)

        reqs = []
        while not ctl.ui_requests.empty():
            reqs.append(ctl.ui_requests.get_nowait())
        self.assertEqual(reqs, [("unknown", ("host", "someblog.net"), so.title), ("checkin",)])
        verdicts = db.execute("SELECT exe, verdict FROM usage_session ORDER BY session_id").fetchall()
        self.assertEqual(verdicts, [("chrome.exe", "unknown"), ("Code.exe", "focus"), ("Code.exe", "away")])
        self.assertEqual(ctl.task_id, cat_app.get_or_create_task(db, cat_app.AUTO_TASK))   # 배운 건 '자동 분류' 한 목록에

    def test_watch_mode_never_asks(self):
        db = connect(":memory:")
        ctl = Control("log")
        ctl.task_id = cat_app.get_or_create_task(db, "파이썬 강의")    # 할 일이 남아 있어도 감시 모드면 안 묻는다
        ctl.allow_items = [("app", "Code.exe")]
        frames = [WindowInfo("질문", "chrome.exe", "stackoverflow.com/q/1")] * 10 \
            + [WindowInfo("main.py", "Code.exe")] * 130
        run(db, RecordingProbe(frames), 10.0, ctl, True)
        self.assertTrue(ctl.ui_requests.empty())

    def test_switch_to_watch_cancels_pending_checkin(self):
        """'어디야?'를 기다리는 중에 감시 모드로 바꾸면 자리 비움으로 세지 않는다."""
        db = connect(":memory:")
        ctl = Control("close")
        ctl.task_id = cat_app.get_or_create_task(db, "파이썬 강의")
        ctl.allow_items = [("app", "Code.exe")]
        code = WindowInfo("main.py", "Code.exe")
        switch = lambda left: setattr(ctl, "action", "log") if left == 20 else None   # 확인 직후 감시 모드로
        run(db, RecordingProbe([code] * 140, on_frame=switch), 10.0, ctl, True)
        self.assertFalse(ctl.checkin_pending)
        self.assertEqual(db.execute("SELECT DISTINCT verdict FROM usage_session").fetchall(), [("focus",)])

    def test_summary_splits_focus_minutes(self):
        db = connect(":memory:")
        t = days_ago(1)
        for verdict, sec in (("focus", 600), ("distract", 120), ("unknown", 60)):
            db.execute("INSERT INTO usage_session (started_at, exe, duration_sec, is_idle, verdict)"
                       " VALUES (?, 'x.exe', ?, 0, ?)", (t, sec, verdict))
        db.commit()
        cat_app.summarize_and_prune(db)
        self.assertEqual(db.execute("SELECT minutes, focus_minutes, distract_minutes, unknown_minutes"
                                    " FROM daily_summary").fetchone(), (13.0, 10.0, 2.0, 1.0))


@unittest.skipUnless(sys.platform == "win32", "Windows 전용")
class TestSingleInstance(unittest.TestCase):
    def test_second_start_is_refused(self):
        name = f"Local\\jipsa-cat-test-{os.getpid()}"
        self.assertTrue(single_instance(name))
        self.assertFalse(single_instance(name))


class TestWatchThreadDoesNotDieSilently(unittest.TestCase):
    """DB 연결 실패가 감시 스레드를 조용히 죽이면 안 된다 — GUI는 계속 떠 있는데 아무 기록도 안 남는 버그였다."""

    def test_connect_failure_is_logged_and_stops(self):
        ctl = Control("log")
        boom = RuntimeError("디스크 꽉 참")
        with unittest.mock.patch.object(cat_app, "connect", side_effect=boom):
            cat_app.watch_in_background(":memory:", 0.0, ctl)   # WindowsProbe()까지 못 감 (connect가 먼저 터짐)
        self.assertIn("RuntimeError", ctl.last)
        self.assertIn("디스크 꽉 참", ctl.last)
        self.assertTrue(ctl.stop.is_set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
