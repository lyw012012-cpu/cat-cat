#!/usr/bin/env python3
"""cat_app DB 계층 테스트 — python test_app.py (Windows 없이 동작)"""

import os
import sqlite3
import sys
import unittest

from cat_app import (Control, block, connect, load_rules, run, save_session,
                     single_instance, top_apps_today)
from watch import Session, WindowInfo, now_iso, pick_rule

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 앱처럼 cp949 콘솔에서 이모지 출력 허용

SHORTS = WindowInfo("웃긴영상 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/shorts/x")
NORMAL = WindowInfo("강의 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/watch?v=a")


class TestDb(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def test_rules_survive_db_round_trip(self):
        """DB에서 읽은 규칙이 스파이크 기본 규칙과 똑같이 판정해야 한다 (AND/OR 그룹 포함)."""
        rules = load_rules(self.db)
        self.assertEqual(pick_rule(rules, SHORTS).action, "close")
        self.assertEqual(pick_rule(rules, NORMAL).action, "warn")

    def test_default_rules_seeded(self):
        n = self.db.execute("SELECT COUNT(*) FROM block_rule").fetchone()[0]
        self.assertEqual(n, 3)

    def test_disabled_rule_is_ignored(self):
        self.db.execute("UPDATE block_rule SET enabled = 0 WHERE action = 'close'")
        self.assertEqual(pick_rule(load_rules(self.db), SHORTS).action, "warn")

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


class TestLiveModeSwitch(unittest.TestCase):
    """실행 중에 창에서 모드를 바꾸면 다음 판정부터 바로 따라야 한다."""

    def test_switch_to_work_mode_mid_run(self):
        ctl = Control("log")
        frames = [SHORTS, NORMAL, WindowInfo("쇼츠2 - YouTube", "chrome.exe", "https://www.youtube.com/shorts/y")]

        class Probe:
            closed = []
            def probe(self):
                if len(frames) == 1:
                    ctl.action = "close"         # 사용자가 창에서 업무모드를 누른 순간
                return frames.pop(0) if frames else None
            def idle_seconds(self):
                return 0.0
            def close_tab(self, hwnd):
                self.closed.append(hwnd); return True

        probe = Probe()
        run(connect(":memory:"), probe, 0.0, ctl, stop_when_empty=True)
        self.assertEqual(len(probe.closed), 1)   # 감시 모드 때 본 첫 쇼츠는 안 닫고, 전환 후 쇼츠만 닫음
        self.assertIn("탭을 닫음", ctl.last)


@unittest.skipUnless(sys.platform == "win32", "Windows 전용")
class TestSingleInstance(unittest.TestCase):
    def test_second_start_is_refused(self):
        name = f"Local\\jipsa-cat-test-{os.getpid()}"
        self.assertTrue(single_instance(name))
        self.assertFalse(single_instance(name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
