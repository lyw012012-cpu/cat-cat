#!/usr/bin/env python3
"""cat_app DB 계층 테스트 — python test_app.py (Windows 없이 동작)"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import cat_app

from cat_app import (Control, block, connect, load_rules, run, save_session,
                     single_instance, top_apps_today)
from watch import Session, WindowInfo, now_iso, pick_rule

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 앱처럼 cp949 콘솔에서 이모지 출력 허용

SHORTS = WindowInfo("웃긴영상 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/shorts/x")
NORMAL = WindowInfo("강의 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/watch?v=a")   # 공부 → 제외
FUN = WindowInfo("웃긴 고양이 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/watch?v=f")


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
        self.assertEqual(pick_rule(load_rules(self.db), SHORTS).rule_id, "r_yt_warn")

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
    """고양이 반응 단계: 말하기 → 소리 끄기 → 기다리게 → 탭 닫기(업무모드만)."""

    def test_decide(self):
        shorts = next(r for r in load_rules(connect(":memory:")) if r.rule_id == "r_shorts")
        work = [cat_app.decide(shorts, lv, "close", 0) for lv in range(5)]
        self.assertEqual(work, ["warn", "mute", "delay", "close", "close"])
        self.assertEqual(cat_app.decide(shorts, 3, "log", 59), "warn")    # 감시 모드: 말하기만
        self.assertEqual(cat_app.decide(shorts, 0, "log", 60), "mute")    # 1시간 넘으면 소리 끔

    def test_shorts_climb_the_ladder_and_mode_switch(self):
        ctl = Control("log")
        shorts = [WindowInfo(f"쇼츠{i}", "chrome.exe", f"youtube.com/shorts/{i}") for i in range(4)]
        frames = shorts + [WindowInfo("main.py - VS Code", "Code.exe")]
        # 첫 쇼츠는 감시 모드에서 보고, 그다음 업무모드로 바꾼다
        probe = RecordingProbe(frames, on_frame=lambda left: setattr(ctl, "action", "close") if left == 4 else None)
        db = connect(":memory:")
        run(db, probe, 0.0, ctl, stop_when_empty=True)

        self.assertEqual(db.execute("SELECT mode, response, executed FROM block_event ORDER BY event_id").fetchall(),
                         [("log", "warn", 1), ("close", "mute", 1), ("close", "delay", 1), ("close", "close", 1)])
        self.assertEqual(probe.calls, ["mute", "close", "unmute"])   # VS Code로 옮기자 소리를 돌려줌
        self.assertEqual(ctl.delay_request, cat_app.DELAY_SEC)       # 기다리게 화면 요청
        self.assertIsNone(ctl.muted_exe)


class TestTimedRule(unittest.TestCase):
    """유튜브(공부·음악 제외)는 오늘 누적 5분 구간마다 한 번 반응한다."""

    def run_on(self, fun_minutes, mode, study_minutes=0):
        db = connect(":memory:")
        t = now_iso()
        save_session(db, Session(t, "chrome.exe", "예능 - YouTube", "youtube.com/watch?v=a", fun_minutes * 60, False, t))
        if study_minutes:
            save_session(db, Session(t, "chrome.exe", "파이썬 강의 - YouTube", "youtube.com/watch?v=s",
                                     study_minutes * 60, False, t))
        probe = RecordingProbe([FUN] * 3)
        ctl = Control(mode)
        run(db, probe, 0.0, ctl, stop_when_empty=True)
        return db, probe, ctl

    def test_once_per_step_in_watch_mode(self):
        db, probe, ctl = self.run_on(30.5, "log")
        self.assertEqual(db.execute("SELECT minutes, response FROM block_event").fetchall(), [(30, "warn")])
        self.assertIn("유튜브 30분째야", ctl.last)

    def test_study_time_is_not_counted(self):
        db, _, _ = self.run_on(7, "log", study_minutes=50)          # 공부 50분은 세지 않음 → 5분 구간 1
        self.assertEqual(db.execute("SELECT minutes FROM block_event").fetchall(), [(5,)])

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
        self.assertEqual(db.execute("SELECT reaction, min_minutes, action FROM block_rule").fetchone(),
                         ("유튜브 {minutes}분째야.", 5, "close"))
        self.assertEqual(db.execute("SELECT operator FROM rule_condition WHERE rule_id = 'r_yt_warn'").fetchall(),
                         [("not_regex",)])                     # v4: 공부·음악 제외 조건이 붙음
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"focus_task", "allow_item", "focus_checkin"} <= tables)   # v5
        db.close()
        connect(path).close()                                  # 두 번 열어도 다시 적용되지 않음


def days_ago(n: int) -> str:
    """n일 전 UTC 03:00 (한국 12:00) — 현지 날짜가 정확히 n일 전이 되게."""
    d = datetime.now(timezone.utc) - timedelta(days=n)
    return d.strftime("%Y-%m-%dT03:00:00Z")


class TestMemoryAndRetention(unittest.TestCase):
    """원본은 90일 뒤 지워도, 하루 요약과 고양이 기억은 남아야 한다."""

    def add(self, db, n_days_ago, host, sec, shorts=0, closed=0):
        t = days_ago(n_days_ago)
        db.execute("INSERT INTO usage_session (started_at, ended_at, exe, url_host, duration_sec, is_idle)"
                   " VALUES (?, ?, 'chrome.exe', ?, ?, 0)", (t, t, host, sec))
        for i in range(shorts):
            db.execute("INSERT INTO block_event (occurred_at, rule_id, action, exe, url_host, mode, executed)"
                       " VALUES (?, 'r_shorts', 'close', 'chrome.exe', ?, 'close', ?)", (t, host, int(i < closed)))
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

    PLAYLIST = "https://www.youtube.com/watch?v=aaa&list=PLz2iXe7EqJOOTNTK27a4-WsgZU5NVfguh&index=3"

    def test_parse_allow(self):
        P = cat_app.parse_allow
        self.assertEqual(P(self.PLAYLIST), ("playlist", "PLz2iXe7EqJOOTNTK27a4-WsgZU5NVfguh"))
        self.assertEqual(P("Code.exe"), ("app", "Code.exe"))
        self.assertEqual(P("https://www.docs.python.org/3/tutorial/"), ("host", "docs.python.org"))
        self.assertEqual(P("SQLD 기출"), ("keyword", "SQLD 기출"))

    def test_playlist_allows_next_video(self):
        items = [cat_app.parse_allow(self.PLAYLIST)]
        nxt = WindowInfo("다음 강의", "chrome.exe",
                         "youtube.com/watch?v=bbb&list=PLz2iXe7EqJOOTNTK27a4-WsgZU5NVfguh&index=4")
        other = WindowInfo("다른 영상", "chrome.exe", "youtube.com/watch?v=ccc")
        self.assertTrue(cat_app.is_allowed(nxt, items))      # v= 가 바뀌어도 list= 가 같으면 허용
        self.assertFalse(cat_app.is_allowed(other, items))

    def test_host_allows_whole_site(self):
        items = [("host", "python.org")]
        self.assertTrue(cat_app.is_allowed(WindowInfo("t", "chrome.exe", "docs.python.org/3/library/re.html"), items))
        self.assertFalse(cat_app.is_allowed(WindowInfo("t", "chrome.exe", "notpython.org/x"), items))

    def test_classify_order(self):
        rules = load_rules(connect(":memory:"))
        so = WindowInfo("질문", "chrome.exe", "stackoverflow.com/q/1")
        self.assertEqual(cat_app.classify(SHORTS, rules, [("host", "youtube.com")], {}), "distract")  # 규칙이 먼저
        self.assertEqual(cat_app.classify(so, rules, [], {}), "unknown")
        self.assertEqual(cat_app.classify(so, rules, [], {("host", "stackoverflow.com"): "focus"}), "focus")
        self.assertEqual(cat_app.classify(so, rules, [("host", "stackoverflow.com")], {}), "focus")

    def test_ask_unknown_then_checkin_then_away(self):
        db = connect(":memory:")
        ctl = Control("close")
        ctl.task_id = cat_app.get_or_create_task(db, "파이썬 강의")
        cat_app.add_allow(db, ctl.task_id, "app", "Code.exe")
        ctl.allow_items = cat_app.load_allow(db, ctl.task_id)
        so = WindowInfo("질문 - Stack Overflow", "chrome.exe", "stackoverflow.com/q/1")
        code = WindowInfo("main.py - VS Code", "Code.exe")
        # 10초 간격: 모르는 창 30초 → 허용된 창 20분 → 대답 없이 3분 넘게 더
        frames = [so] * 3 + [code] * 120 + [code] * 20
        run(db, RecordingProbe(frames), 10.0, ctl, stop_when_empty=True)

        reqs = []
        while not ctl.ui_requests.empty():
            reqs.append(ctl.ui_requests.get_nowait())
        self.assertEqual(reqs, [("unknown", ("host", "stackoverflow.com"), so.title), ("checkin",)])
        verdicts = db.execute("SELECT exe, verdict FROM usage_session ORDER BY session_id").fetchall()
        self.assertEqual(verdicts, [("chrome.exe", "unknown"), ("Code.exe", "focus"), ("Code.exe", "away")])
        self.assertEqual(db.execute("SELECT DISTINCT task_id FROM usage_session").fetchall(), [(ctl.task_id,)])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
